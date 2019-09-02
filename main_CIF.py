#!/usr/bin/env python

from datetime import datetime
from time import time
import os
import tensorflow as tf
import numpy as np

from utils.arguments import args
from utils.dataset import ASR_align_DataSet, TextDataSet
from utils.tools import TFData, gradient_penalty, CE_loss, decode, get_predicts, batch_cer

from models.GAN import PhoneClassifier, PhoneDiscriminator3
from models.CIF import attentionAssign, CIF
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
tf.random.set_seed(args.seed)

def train():
    with tf.device("/cpu:0"):
        dataset_train = ASR_align_DataSet(
            trans_file=args.dirs.train.trans,
            align_file=None,
            uttid2wav=args.dirs.train.wav_scp,
            feat_len_file=args.dirs.train.feat_len,
            args=args,
            _shuffle=True,
            transform=True)
        dataset_dev = ASR_align_DataSet(
            trans_file=args.dirs.dev.trans,
            align_file=None,
            uttid2wav=args.dirs.dev.wav_scp,
            feat_len_file=args.dirs.dev.feat_len,
            args=args,
            _shuffle=False,
            transform=True)
        # dataset_train_supervise = ASR_align_DataSet(
        #     trans_file=args.dirs.train_supervise.trans,
        #     align_file=None,
        #     uttid2wav=args.dirs.train_supervise.wav_scp,
        #     feat_len_file=args.dirs.train_supervise.feat_len,
        #     args=args,
        #     _shuffle=False,
        #     transform=True)
        # feature_train_supervise = TFData(dataset=dataset_train_supervise,
        #                 dir_save=args.dirs.train_supervise.tfdata,
        #                 args=args).read()
        feature_train = TFData(dataset=dataset_train,
                        dir_save=args.dirs.train.tfdata,
                        args=args).read()
        feature_dev = TFData(dataset=dataset_dev,
                        dir_save=args.dirs.dev.tfdata,
                        args=args).read()
        # supervise_uttids, supervise_x = next(iter(feature_train_supervise.take(args.num_supervised).\
        #     padded_batch(args.num_supervised, ((), [None, args.dim_input]))))

        iter_feature_train = iter(feature_train.cache().repeat().shuffle(500).padded_batch(args.batch_size,
                ((), [None, args.dim_input])).prefetch(buffer_size=5))
        feature_dev = feature_dev.padded_batch(args.batch_size, ((), [None, args.dim_input]))

        # dataset_text = TextDataSet(list_files=[args.dirs.lm.data],
        #                            args=args, _shuffle=True)
        # tfdata_train = tf.data.Dataset.from_generator(
        #     dataset_text, (tf.int32), (tf.TensorShape([None])))
        # iter_text = iter(tfdata_train.cache().repeat().shuffle(1000).map(
        #     lambda x: x[:args.max_label_len]).padded_batch(args.batch_size, ([args.max_label_len])).prefetch(buffer_size=5))


    # create model paremeters
    assigner = attentionAssign(args)
    G = PhoneClassifier(args)
    D = PhoneDiscriminator3(args)
    assigner.summary()
    G.summary()
    D.summary()

    optimizer_G = tf.keras.optimizers.Adam(args.opti.G.lr, beta_1=0.5, beta_2=0.9)
    optimizer_D = tf.keras.optimizers.Adam(args.opti.D.lr, beta_1=0.5, beta_2=0.9)

    writer = tf.summary.create_file_writer(str(args.dir_log))
    ckpt = tf.train.Checkpoint(G=G, optimizer_G=optimizer_G)
    ckpt_manager = tf.train.CheckpointManager(ckpt, args.dir_checkpoint, max_to_keep=20)
    step = 0
    step1 = 0

    # if a checkpoint exists, restore the latest checkpoint.
    if args.dirs.checkpoint:
        _ckpt_manager = tf.train.CheckpointManager(ckpt, args.dirs.checkpoint, max_to_keep=1)
        ckpt.restore(_ckpt_manager.latest_checkpoint)
        print('checkpoint {} restored!!'.format(_ckpt_manager.latest_checkpoint))
        step = int(_ckpt_manager.latest_checkpoint.split('-')[-1])

    start_time = datetime.now()
    num_processed = 0
    progress = 0

    # while step1 < 9999:
    #     uttids, x = next(iter_feature_train)
    #     alpha = assigner(x)
    #
    #     step1 += 1

    while step < 99999999:
        start = time()

        # for _ in range(args.opti.D_G_rate):
        #     uttids, x = next(iter_feature_train)
        #     text = next(iter_text)
        #     P_Real = tf.one_hot(text, args.dim_output)
        #     cost_D, gp = train_D(x, stamps[:, :args.max_label_len], P_Real, text>0,
        #                          assigner, G, D, optimizer_D, args.lambda_gp)
        #
        uttids, x = next(iter_feature_train)
        # y = dataset_train.get_attrs('trans', uttids.numpy(), args.max_label_len)
        y = dataset_train.get_attrs('trans', uttids.numpy())
        # cost_G, fs = train_G(x, stamps[:, :args.max_label_len],
        #                      assigner, G, D, optimizer_G, args.lambda_fs)
        # loss_supervise = 0
        cost_G = cost_D = 0
        ce_loss_supervise, quantity_loss_supervise = train_G_supervised(
            x, y, assigner, G, optimizer_G, args.dim_output, args.lambda_supervision)

        num_processed += len(x)
        progress = num_processed / args.data.train_size
        if step % 1 == 0:
            print('cost_G: {:.3f}\tcost_D: {:.3f}\tloss_supervise: {:.3f}|{:.3f}\tbatch: {}|{}\tused: {:.3f}\t {:.3f}% iter: {}'.format(
                   cost_G, cost_D, ce_loss_supervise, quantity_loss_supervise, x.shape, None, time()-start, progress*100.0, step))
            # with writer.as_default():
            #     tf.summary.scalar("costs/cost_G", cost_G, step=step)
            #     tf.summary.scalar("costs/cost_D", cost_D, step=step)
            #     tf.summary.scalar("costs/gp", gp, step=step)
            #     tf.summary.scalar("costs/loss_supervise", loss_supervise, step=step)
        if step % args.dev_step == 0:
            cer = evaluate(feature_dev, dataset_dev, args.data.dev_size, assigner, G)
            with writer.as_default():
                tf.summary.scalar("performance/cer", cer, step=step)
        if step % args.decode_step == 0:
            monitor(dataset_dev[0], assigner, G)
        # if step % args.save_step == 0:
        #     save_path = ckpt_manager.save(step)
        #     print('save model {}'.format(save_path))

        step += 1

    print('training duration: {:.2f}h'.format((datetime.now()-start_time).total_seconds()/3600))


def train_G(x, stamps, assigner, G, D, optimizer_G, lambda_fs):
    params_G = G.trainable_variables + assigner.trainable_variables
    with tf.GradientTape(watch_accessed_variables=False) as tape_G:
        tape_G.watch(params_G)
        logits = G(x, training=True)
        P_G = tf.nn.softmax(logits)
        disc_fake = D(P_G, training=True)

        gen_cost = -tf.reduce_mean(disc_fake)

    gradients_G = tape_G.gradient(gen_cost, params_G)
    optimizer_G.apply_gradients(zip(gradients_G, params_G))

    return gen_cost


def train_D(x, stamps, P_Real, mask_real, assigner, G, D, optimizer_D, lambda_gp):
    params_D = D.trainable_variables
    with tf.GradientTape(watch_accessed_variables=False) as tape_D:
        tape_D.watch(params_D)

        alpha = assigner(x)
        l = CIF(x, alpha, threshold=args.model.G.threshold)
        logits= G(l, training=True)
        P_G = tf.nn.softmax(logits)
        disc_real = D(P_Real, training=True) # to be +inf
        disc_fake = D(P_G, training=True) # to be -inf

        disc_cost = tf.reduce_mean(disc_fake) - tf.reduce_mean(disc_real)
        gp = gradient_penalty(D, P_Real, P_G)
        disc_cost += lambda_gp * gp

    gradients_D = tape_D.gradient(disc_cost, params_D)
    optimizer_D.apply_gradients(zip(gradients_D, params_D))

    return disc_cost, gp


# @tf.function
def train_G_supervised(x, y, assigner, G, optimizer, dim_output, lambda_supervision):
    vars = G.trainable_variables + assigner.trainable_variables
    with tf.GradientTape() as tape:
        alpha = assigner(x)
        musk = tf.cast(tf.reduce_sum(tf.abs(x), -1) > 0, tf.float32)
        alpha *= musk
        # sum
        _num = tf.reduce_sum(alpha, -1)
        # scaling
        num = tf.reduce_sum(tf.cast(y>0, tf.float32), -1)
        alpha *= tf.tile((num/_num)[:, None], [1, alpha.shape[1]])

        # l = CIF(x, alpha, threshold=args.model.G.threshold, max_label_len=args.max_label_len)
        l = CIF(x, alpha, threshold=args.model.attention.threshold)

        logits= G(l, training=True)
        ce_loss = CE_loss(logits, y, dim_output, confidence=0.9)

        quantity_loss = tf.reduce_mean(tf.losses.mean_squared_error(_num, num))

        loss = ce_loss + quantity_loss * 1.0

    gradients = tape.gradient(loss, vars)
    optimizer.apply_gradients(zip(gradients, vars))

    return ce_loss, quantity_loss


def Decode(save_file):
    dataset = ASR_align_DataSet(
        trans_file=args.dirs.train.trans,
        align_file=None,
        uttid2wav=args.dirs.train.wav_scp,
        feat_len_file=args.dirs.train.feat_len,
        args=args,
        _shuffle=False,
        transform=True)
    dataset_dev = ASR_align_DataSet(
        trans_file=args.dirs.dev.trans,
        align_file=None,
        uttid2wav=args.dirs.dev.wav_scp,
        feat_len_file=args.dirs.dev.feat_len,
        args=args,
        _shuffle=False,
        transform=True)

    feature_dev = TFData(dataset=dataset_dev,
                    dir_save=args.dirs.dev.tfdata,
                    args=args).read()
    feature_dev = feature_dev.padded_batch(args.batch_size, ((), [None, args.dim_input]))

    G = PhoneClassifier(args)
    G.summary()

    optimizer_G = tf.keras.optimizers.Adam(1e-4)
    ckpt = tf.train.Checkpoint(G=G, optimizer_G=optimizer_G)
    ckpt_manager = tf.train.CheckpointManager(ckpt, args.dirs.checkpoint, max_to_keep=1)
    ckpt.restore(ckpt_manager.latest_checkpoint)
    print ('checkpoint {} restored!!'.format(ckpt_manager.latest_checkpoint))
    fer, cer = evaluate(feature_dev, dataset_dev, args.data.dev_size, G)
    decode(dataset, G, args.idx2token, 'output/'+save_file)


def evaluate(feature, dataset, dev_size, assigner, model):
    num_processed = 0
    total_cer_dist = 0
    total_cer_len = 0
    for batch in feature:
        uttids, x = batch
        # trans = dataset.get_attrs('trans', uttids.numpy(), args.max_label_len)
        trans = dataset.get_attrs('trans', uttids.numpy())

        alpha = assigner(x)
        musk = tf.cast(tf.reduce_sum(tf.abs(x), -1) > 0, tf.float32)
        alpha *= musk
        logits = model(x)
        preds = get_predicts(logits)

        batch_cer_dist, batch_cer_len = batch_cer(preds.numpy(), trans)
        total_cer_dist += batch_cer_dist
        total_cer_len += batch_cer_len

        num_processed += len(x)

    cer = total_cer_dist/total_cer_len
    print('dev PER: {:.3f}\t {} / {}'.format(cer, num_processed, dev_size))

    return cer


def monitor(sample, assigner, model):
    x = np.array([sample['feature']], dtype=np.float32)
    alpha = assigner(x)
    # l = CIF(x, alpha, threshold=args.model.G.threshold, max_label_len=args.max_label_len)
    l = CIF(x, alpha, threshold=args.model.G.threshold)
    logits = model(l)
    predicts = get_predicts(logits)
    print('predicts: \n', predicts.numpy()[0])
    print('trans: \n', sample['trans'])


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument('-m', type=str, dest='mode', default='train')
    parser.add_argument('--name', type=str, dest='name', default=None)
    parser.add_argument('--gpu', type=str, dest='gpu', default=0)
    parser.add_argument('-c', type=str, dest='config')

    param = parser.parse_args()

    print('CUDA_VISIBLE_DEVICES: ', param.gpu)
    os.environ["CUDA_VISIBLE_DEVICES"] = param.gpu
    gpus = tf.config.experimental.list_physical_devices('GPU')
    assert len(gpus) > 0, "Not enough GPU hardware devices available"
    [tf.config.experimental.set_memory_growth(gpu, True) for gpu in gpus]

    if param.name:
        args.dir_exps = args.dir_exps /  param.name
        args.dir_log = args.dir_exps / 'log'
        args.dir_checkpoint = args.dir_exps / 'checkpoint'
        if not args.dir_exps.is_dir(): args.dir_exps.mkdir()
        if not args.dir_log.is_dir(): args.dir_log.mkdir()
        if not args.dir_checkpoint.is_dir(): args.dir_checkpoint.mkdir()
        with open(args.dir_exps / 'configs.txt', 'w') as fw:
            print(args, file=fw)

    if param.mode == 'train':
        print('enter the TRAINING phrase')
        train()

    elif param.mode == 'decode':
        """
        python main_supervise.py -m decode --name timit_supervised2_decode.txt --gpu 0 -c configs/timit_supervised2.yaml
        """
        print('enter the DECODING phrase')
        assert args.dirs.checkpoint
        assert param.name
        Decode(param.name)


        # python ../../main.py -m save --gpu 1 --name kin_asr -c configs/rna_char_big3.yaml