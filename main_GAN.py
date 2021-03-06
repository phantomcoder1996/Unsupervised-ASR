#!/usr/bin/env python

from datetime import datetime
from time import time
import os
os.environ['TF_CPP_MIN_LOG_LEVEL']='0'
import tensorflow as tf

from utils.arguments import args
from utils.dataset import ASR_align_DataSet, TextDataSet
from utils.tools import TFData, gradient_penalty, frames_constrain_loss, stamps2indices,\
    CE_loss, evaluate, monitor, decode, bounds2stamps

from models.GAN import PhoneClassifier, PhoneDiscriminator3
# from models.GAN import PhoneClassifier2 as PhoneClassifier

tf.random.set_seed(args.seed)

def train():
    with tf.device("/cpu:0"):
        dataset_train = ASR_align_DataSet(
            trans_file=args.dirs.train.trans,
            align_file=args.dirs.train.align,
            uttid2wav=args.dirs.train.wav_scp,
            feat_len_file=args.dirs.train.feat_len,
            args=args,
            _shuffle=True,
            transform=True)
        dataset_dev = ASR_align_DataSet(
            trans_file=args.dirs.dev.trans,
            align_file=args.dirs.dev.align,
            uttid2wav=args.dirs.dev.wav_scp,
            feat_len_file=args.dirs.dev.feat_len,
            args=args,
            _shuffle=False,
            transform=True)
        dataset_train_supervise = ASR_align_DataSet(
            trans_file=args.dirs.train_supervise.trans,
            align_file=args.dirs.train_supervise.align,
            uttid2wav=args.dirs.train_supervise.wav_scp,
            feat_len_file=args.dirs.train_supervise.feat_len,
            args=args,
            _shuffle=False,
            transform=True)
        feature_train_supervise = TFData(dataset=dataset_train_supervise,
                        dir_save=args.dirs.train_supervise.tfdata,
                        args=args).read()
        feature_train = TFData(dataset=dataset_train,
                        dir_save=args.dirs.train.tfdata,
                        args=args).read()
        feature_dev = TFData(dataset=dataset_dev,
                        dir_save=args.dirs.dev.tfdata,
                        args=args).read()
        supervise_uttids, supervise_x = next(iter(feature_train_supervise.take(args.num_supervised).\
            padded_batch(args.num_supervised, ((), [None, args.dim_input]))))
        supervise_aligns = dataset_train_supervise.get_attrs('align', supervise_uttids.numpy())
        supervise_bounds = dataset_train_supervise.get_attrs('bounds', supervise_uttids.numpy())


        iter_feature_train = iter(feature_train.cache().repeat().shuffle(500).padded_batch(args.batch_size,
                ((), [None, args.dim_input])).prefetch(buffer_size=5))
        feature_dev = feature_dev.padded_batch(args.batch_size, ((), [None, args.dim_input]))

        dataset_text = TextDataSet(list_files=[args.dirs.lm.data],
                                   args=args, _shuffle=True)
        tfdata_train = tf.data.Dataset.from_generator(
            dataset_text, (tf.int32), (tf.TensorShape([None])))
        iter_text = iter(tfdata_train.cache().repeat().shuffle(1000).map(
            lambda x: x[:args.model.D.max_label_len]).padded_batch(args.batch_size, ([args.model.D.max_label_len])).prefetch(buffer_size=5))


    # create model paremeters
    G = PhoneClassifier(args)
    D = PhoneDiscriminator3(args)
    G.summary()
    D.summary()

    optimizer_G = tf.keras.optimizers.Adam(args.opti.G.lr, beta_1=0.5, beta_2=0.9)
    optimizer_D = tf.keras.optimizers.Adam(args.opti.D.lr, beta_1=0.5, beta_2=0.9)

    writer = tf.summary.create_file_writer(str(args.dir_log))
    ckpt = tf.train.Checkpoint(G=G, optimizer_G=optimizer_G)
    ckpt_manager = tf.train.CheckpointManager(ckpt, args.dir_checkpoint, max_to_keep=20)
    step = 0

    # if a checkpoint exists, restore the latest checkpoint.
    if args.dirs.checkpoint:
        _ckpt_manager = tf.train.CheckpointManager(ckpt, args.dirs.checkpoint, max_to_keep=1)
        ckpt.restore(_ckpt_manager.latest_checkpoint)
        print('checkpoint {} restored!!'.format(_ckpt_manager.latest_checkpoint))
        step = int(_ckpt_manager.latest_checkpoint.split('-')[-1])

    start_time = datetime.now()
    num_processed = 0
    progress = 0

    while step < 99999999:
        start = time()

        for _ in range(args.opti.D_G_rate):
            uttids, x = next(iter_feature_train)
            stamps = dataset_train.get_attrs('stamps', uttids.numpy())
            text = next(iter_text)
            P_Real = tf.one_hot(text, args.dim_output)
            cost_D, gp = train_D(x, stamps, P_Real, text>0, G, D, optimizer_D,
                                 args.lambda_gp, args.model.D.max_label_len)
            # cost_D, gp = train_D(x, P_Real, text>0, G, D, optimizer_D,
            #                      args.lambda_gp, args.model.G.len_seq, args.model.D.max_label_len)

        uttids, x = next(iter_feature_train)
        stamps = dataset_train.get_attrs('stamps', uttids.numpy())
        cost_G, fs = train_G(x, stamps, G, D, optimizer_G, args.lambda_fs)
        # cost_G, fs = train_G(x, G, D, optimizer_G,
        #                      args.lambda_fs, args.model.G.len_seq, args.model.D.max_label_len)

        loss_supervise = train_G_supervised(supervise_x, supervise_aligns, G, optimizer_G, args.dim_output, args.lambda_supervision)
        # loss_supervise, bounds_loss = train_G_bounds_supervised(
        #     supervise_x, supervise_bounds, supervise_aligns, G, optimizer_G, args.dim_output)

        num_processed += len(x)
        progress = num_processed / args.data.train_size
        if step % 10 == 0:
            print('cost_G: {:.3f}|{:.3f}\tcost_D: {:.3f}|{:.3f}\tloss_supervise: {:.3f}\tbatch: {}|{}\tused: {:.3f}\t {:.3f}% iter: {}'.format(
                   cost_G, fs, cost_D, gp, loss_supervise, x.shape, text.shape, time()-start, progress*100.0, step))
            with writer.as_default():
                tf.summary.scalar("costs/cost_G", cost_G, step=step)
                tf.summary.scalar("costs/cost_D", cost_D, step=step)
                tf.summary.scalar("costs/gp", gp, step=step)
                tf.summary.scalar("costs/fs", fs, step=step)
                tf.summary.scalar("costs/loss_supervise", loss_supervise, step=step)
        if step % args.dev_step == 0:
            # fer, cer = evaluate(feature_dev, dataset_dev, args.data.dev_size, G)
            fer, cer_0 = evaluate(feature_dev, dataset_dev, args.data.dev_size, G, beam_size=0, with_stamp=True)
            fer, cer = evaluate(feature_dev, dataset_dev, args.data.dev_size, G, beam_size=0, with_stamp=False)
            with writer.as_default():
                tf.summary.scalar("performance/fer", fer, step=step)
                tf.summary.scalar("performance/cer_0", cer_0, step=step)
                tf.summary.scalar("performance/cer", cer, step=step)
        if step % args.decode_step == 0:
            monitor(dataset_dev[0], G)
        if step % args.save_step == 0:
            save_path = ckpt_manager.save(step)
            print('save model {}'.format(save_path))

        step += 1

    print('training duration: {:.2f}h'.format((datetime.now()-start_time).total_seconds()/3600))


def train_G(x, stamps, G, D, optimizer_G, lambda_fs):
    indices = stamps2indices(stamps)
    params_G = G.trainable_variables
    with tf.GradientTape(watch_accessed_variables=False) as tape_G:
        tape_G.watch(params_G)
        logits = G(x, training=True)
        P_G = tf.nn.softmax(logits)
        _P_G = tf.gather_nd(P_G, indices)
        # disc_fake = D([_P_G, aligns>0], training=True)
        disc_fake = D(_P_G, training=True)

        gen_cost = -tf.reduce_mean(disc_fake)
        # gen_cost = tf.reduce_mean(tf.math.squared_difference(disc_fake, 1))
        fs = frames_constrain_loss(logits, stamps)
        # fs = 0
        gen_cost += lambda_fs * fs

    gradients_G = tape_G.gradient(gen_cost, params_G)
    optimizer_G.apply_gradients(zip(gradients_G, params_G))

    return gen_cost, fs

# def train_G(x, G, D, optimizer_G, lambda_fs, len_G, len_D):
#     cut_idx = tf.random.uniform((), minval=0, maxval=len_G, dtype=tf.dtypes.int32).numpy()
#     num_split = int(len_D // len_G)
#     list_tensors = tf.split(x[:, cut_idx:(cut_idx + len_D), :], num_split, axis=1)
#     x = tf.concat(list_tensors, 0)
#
#     params_G = G.trainable_variables[:-2]
#     # ['dense_1/kernel:0', 'dense_1/bias:0']
#     with tf.GradientTape(watch_accessed_variables=False) as tape_G:
#         tape_G.watch(params_G)
#         logits, logits_bounds = G(x, training=True)
#
#         # logits
#         list_tensors = tf.split(logits, num_split, axis=0)
#         logits = tf.concat(list_tensors, 1)
#         list_tensors = tf.split(logits_bounds, num_split, axis=0)
#         logits_bounds = tf.concat(list_tensors, 1)
#
#         P_G = tf.nn.softmax(logits)
#         bounds = tf.argmax(logits_bounds, axis=-1, output_type=tf.int32)
#         stamps = bounds2stamps(bounds)
#         indices = stamps2indices(stamps)
#         _P_G = tf.gather_nd(P_G, indices)
#         # disc_fake = D([_P_G, aligns>0], training=True)
#         disc_fake = D(_P_G, training=True)
#
#         gen_cost = -tf.reduce_mean(disc_fake)
#         # gen_cost = tf.reduce_mean(tf.math.squared_difference(disc_fake, 1))
#         fs = frames_constrain_loss(logits, stamps)
#         # fs = 0
#         gen_cost += lambda_fs * fs
#
#     gradients_G = tape_G.gradient(gen_cost, params_G)
#     optimizer_G.apply_gradients(zip(gradients_G, params_G))
#
#     return gen_cost, fs


def train_D(x, stamps, P_Real, mask_real, G, D, optimizer_D, lambda_gp, len_D):
    indices = stamps2indices(stamps[:, :len_D])
    params_D = D.trainable_variables
    with tf.GradientTape(watch_accessed_variables=False) as tape_D:
        tape_D.watch(params_D)
        logits = G(x, training=True)
        P_G = tf.nn.softmax(logits)
        _P_G = tf.gather_nd(P_G, indices)
        disc_real = D(P_Real, training=True) # to be +inf
        disc_fake = D(_P_G, training=True) # to be -inf

        disc_cost = tf.reduce_mean(disc_fake) - tf.reduce_mean(disc_real)
        gp = gradient_penalty(D, P_Real, _P_G)
        disc_cost += lambda_gp * gp

    gradients_D = tape_D.gradient(disc_cost, params_D)
    optimizer_D.apply_gradients(zip(gradients_D, params_D))

    return disc_cost, gp


# def train_D(x, P_Real, mask_real, G, D, optimizer_D, lambda_gp, len_G, len_D):
#     cut_idx = tf.random.uniform((), minval=0, maxval=5, dtype=tf.dtypes.int32).numpy()
#     params_D = D.trainable_variables
#     with tf.GradientTape(watch_accessed_variables=False) as tape_D:
#         tape_D.watch(params_D)
#         logits, logits_bounds = G(x, training=True)
#         bounds = tf.argmax(logits_bounds, axis=-1, output_type=tf.int32)
#         stamps = bounds2stamps(bounds)
#         indices = stamps2indices(stamps)
#         P_G = tf.nn.softmax(logits)
#         _P_G = tf.gather_nd(P_G, indices)[:, cut_idx:(cut_idx + len_D), :]
#         disc_real = D(P_Real, training=True) # to be +inf
#         disc_fake = D(_P_G, training=True) # to be -inf
#
#         disc_cost = tf.reduce_mean(disc_fake) - tf.reduce_mean(disc_real)
#         gp = gradient_penalty(D, P_Real, _P_G)
#         disc_cost += lambda_gp * gp
#
#     gradients_D = tape_D.gradient(disc_cost, params_D)
#     optimizer_D.apply_gradients(zip(gradients_D, params_D))
#
#     return disc_cost, gp


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
        align_file=args.dirs.dev.align,
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


# @tf.function
def train_G_supervised(x, aligns, G, optimizer, dim_output, lambda_supervision):
    with tf.GradientTape() as tape_G:
        logits = G(x, training=True)
        ce_loss = CE_loss(logits, aligns, dim_output, confidence=0.8)
        loss = ce_loss * lambda_supervision

    gradients_G = tape_G.gradient(loss, G.trainable_variables)
    optimizer.apply_gradients(zip(gradients_G, G.trainable_variables))

    return ce_loss


def train_G_bounds_supervised(x, bounds, labels, model, optimizer_G, dim_output):
    """
    random cut head & make it can be split evenly
    """
    len_seq = args.model.G.len_seq
    cut_idx = tf.random.uniform((), minval=0, maxval=len_seq, dtype=tf.dtypes.int32).numpy()
    num_split = int((x.shape[1]-cut_idx) // len_seq)
    max_idx = cut_idx + num_split * len_seq

    # reshape x
    x_pad = tf.pad(x[:, cut_idx:max_idx, :], [[0,0], [0,tf.reduce_max([max_idx-x.shape[1], 0])], [0,0]])
    x_reshaped = tf.concat(tf.split(x_pad, num_split, axis=1), 0)

    bounds_pad = tf.pad(bounds[:, cut_idx:max_idx], [[0,0], [0,tf.reduce_max([max_idx-x.shape[1], 0])]])
    bounds_reshaped = tf.concat(tf.split(bounds_pad, num_split, axis=1), 0)
    labels_pad = tf.pad(labels[:, cut_idx:max_idx], [[0,0], [0,tf.reduce_max([max_idx-x.shape[1], 0])]])
    labels_reshaped = tf.concat(tf.split(labels_pad, num_split, axis=1), 0)

    with tf.GradientTape() as tape_G:
        logits, logits_bounds = model(x_reshaped, training=True)
        ce_loss = CE_loss(logits, labels_reshaped, dim_output, confidence=0.8)
        bounds_loss = CE_loss(logits_bounds, bounds_reshaped, 2, confidence=0.8)
        # bounds_loss = 0
        gen_loss = ce_loss + bounds_loss

    gradients_G = tape_G.gradient(gen_loss, model.trainable_variables)
    optimizer_G.apply_gradients(zip(gradients_G, model.trainable_variables))

    return gen_loss, bounds_loss


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
