#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import os
import sys

LOG_LEVEL_INDEX = sys.argv.index('--log_level') + 1 if '--log_level' in sys.argv else 0
DESIRED_LOG_LEVEL = sys.argv[LOG_LEVEL_INDEX] if 0 < LOG_LEVEL_INDEX < len(sys.argv) else '3'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = DESIRED_LOG_LEVEL

import absl.app
import itertools
import json
import math
import numpy as np
import progressbar
import shutil
import tensorflow as tf
import tensorflow.compat.v1 as tfv1
import time

tfv1.logging.set_verbosity({
    '0': tfv1.logging.DEBUG,
    '1': tfv1.logging.INFO,
    '2': tfv1.logging.WARN,
    '3': tfv1.logging.ERROR
}.get(DESIRED_LOG_LEVEL))

from datetime import datetime
from ds_ctcdecoder import ctc_beam_search_decoder, Scorer
from evaluate import evaluate
from novograd import NovoGrad
from six.moves import zip, range
from util.config import Config, initialize_globals
from util.checkpoints import load_or_init_graph
from util.feeding import create_dataset, samples_to_mfccs, audiofile_to_features
from util.flags import create_flags, FLAGS
from util.helpers import check_ctcdecoder_version, ExceptionBox
from util.logging import log_info, log_error, log_debug, log_progress, create_progressbar

check_ctcdecoder_version()

def variable_on_cpu(name, shape, initializer):
    r"""
    Next we concern ourselves with graph creation.
    However, before we do so we must introduce a utility function ``variable_on_cpu()``
    used to create a variable in CPU memory.
    """
    # Use the /cpu:0 device for scoped operations
    with tf.device(Config.cpu_device):
        # Create or get apropos variable
        var = tfv1.get_variable(name=name, shape=shape, initializer=initializer)
    return var


def conv_sep_1d(x, in_channels, out_channels, kernel_size, stride=1, dilation=1, padding='SAME'):
    depthwise_filter = variable_on_cpu('conv1_filter', [kernel_size, 1, in_channels], tf.contrib.layers.xavier_initializer())
    pointwise_filter = variable_on_cpu('conv2_filter', [1, in_channels, out_channels], tf.contrib.layers.xavier_initializer())

    x = tf.nn.conv1d(x, depthwise_filter, stride=stride, dilations=dilation, padding=padding)
    x = tf.nn.conv1d(x, pointwise_filter, stride=1, dilations=1, padding=padding)
    return x


def conv_1d(x, in_channels, out_channels, kernel_size, stride=1, dilation=1, padding='SAME', use_bias=False):
    conv_filt = variable_on_cpu('conv_filter', [kernel_size, in_channels, out_channels], tf.contrib.layers.xavier_initializer())

    x = tf.nn.conv1d(x, conv_filt, stride=stride, dilations=dilation, padding=padding)

    if use_bias:
        conv_bias = variable_on_cpu('conv_bias', [out_channels], tf.zeros_initializer())
        x = tf.nn.bias_add(x, conv_bias)

    return x


def batch_norm(x, is_training):
    return tf.layers.batch_normalization(x, axis=-1, momentum=0.9, epsilon=1e-3, training=is_training)


def get_same_padding(kernel_size, stride, dilation):
    if dilation > 1:
        return (dilation * kernel_size) // 2 - 1
    return kernel_size // 2


def get_new_lengths(orig_lengths, kernel_size, stride=1, dilation=1):
    padding = get_same_padding(kernel_size, stride, dilation)
    return ((orig_lengths + 2 * padding - dilation * (kernel_size - 1) - 1) // stride + 1)


def pairwise(iterable):
    "s -> (s0,s1), (s1,s2), (s2, s3), ..."
    a, b = itertools.tee(iterable)
    next(b, None)
    return zip(a, b)


def create_model(x, lengths, is_training, n_steps=None):
    with tfv1.variable_scope('block_0'):
        x = conv_sep_1d(x, Config.n_input, Config.n_hidden_1, kernel_size=33, stride=2)
        x = batch_norm(x, is_training)
        x = tf.nn.relu(x)

    lengths = get_new_lengths(lengths, kernel_size=33, stride=2)
    # n_steps is used when creating the export graph so that the sequence mask
    # has the appropriate shape matching the fixed number of steps used there.
    # We need to adjust its value due to the stride=2 of the first block.
    if n_steps != None:
        n_steps = get_new_lengths(n_steps, kernel_size=33, stride=2)
    mask = tf.expand_dims(tf.sequence_mask(lengths, maxlen=n_steps, dtype=tf.float32), axis=-1)

    blocks = ([{'kernel_size': 33, 'out_channels': Config.n_hidden_1}] +
              [{'kernel_size': 33, 'out_channels': Config.n_hidden_1} for _ in range(3)] +
              [{'kernel_size': 39, 'out_channels': Config.n_hidden_1} for _ in range(3)] +
              [{'kernel_size': 51, 'out_channels': Config.n_hidden_2} for _ in range(3)] +
              [{'kernel_size': 63, 'out_channels': Config.n_hidden_2} for _ in range(3)] +
              [{'kernel_size': 75, 'out_channels': Config.n_hidden_2} for _ in range(3)])

    for i, (prev, cur) in enumerate(pairwise(blocks), start=1):
        with tfv1.variable_scope('block_{}'.format(i)):
            res_x = x * mask

            for j in range(5):
                with tfv1.variable_scope('repeat_{}'.format(j)):
                    x = x * mask
                    x = conv_sep_1d(x, x.shape[-1], cur['out_channels'], kernel_size=cur['kernel_size'])
                    x = batch_norm(x, is_training)
                    if j != 4:
                        x = tf.nn.relu(x)

            with tfv1.variable_scope('residual'):
                res_x = conv_1d(res_x, prev['out_channels'], cur['out_channels'], kernel_size=1)
                res_x = batch_norm(res_x, is_training)

            x = x + res_x
            x = tf.nn.relu(x)

    with tfv1.variable_scope('block_16'):
        x = conv_sep_1d(x, Config.n_hidden_2, Config.n_hidden_2, kernel_size=87, dilation=2)
        x = batch_norm(x, is_training)
        x = tf.nn.relu(x)

    with tfv1.variable_scope('block_17'):
        x = conv_1d(x, Config.n_hidden_2, Config.n_hidden_3, kernel_size=1)
        x = batch_norm(x, is_training)
        x = tf.nn.relu(x)

    with tfv1.variable_scope('to_logits'):
        x = conv_1d(x, Config.n_hidden_3, Config.n_hidden_out, kernel_size=1, use_bias=True)

    # total_parameters = 0
    # for variable in tf.trainable_variables():
    #     # shape is an array of tf.Dimension
    #     shape = variable.get_shape()
    #     print(shape)
    #     variable_parameters = 1
    #     for dim in shape:
    #         variable_parameters *= dim.value
    #     total_parameters += variable_parameters
    # print(f'TOTAL: {total_parameters}')
    # sys.exit(0)

    return x, tf.identity(lengths, name='encoded_lengths')


# Accuracy and Loss
# =================

# In accord with 'Deep Speech: Scaling up end-to-end speech recognition'
# (http://arxiv.org/abs/1412.5567),
# the loss function used by our network should be the CTC loss function
# (http://www.cs.toronto.edu/~graves/preprint.pdf).
# Conveniently, this loss function is implemented in TensorFlow.
# Thus, we can simply make use of this implementation to define our loss.

def calculate_mean_edit_distance_and_loss(iterator, dropout, reuse, is_training):
    r'''
    This routine beam search decodes a mini-batch and calculates the loss and mean edit distance.
    Next to total and average loss it returns the mean edit distance,
    the decoded result and the batch's original Y.
    '''
    # Obtain the next batch of data
    batch_filenames, (batch_x, batch_seq_len), batch_y = iterator.get_next()

    # Calculate the logits of the batch
    with tfv1.variable_scope('model', reuse=reuse):
        logits, encoded_lens = create_model(batch_x, batch_seq_len, is_training)

    # Compute the CTC loss using TensorFlow's `ctc_loss`
    total_loss = tfv1.nn.ctc_loss(labels=batch_y, inputs=logits, sequence_length=encoded_lens, time_major=False)

    # Check if any files lead to non finite loss
    non_finite_files = tf.gather(batch_filenames, tfv1.where(~tf.math.is_finite(total_loss)))

    # Calculate the average loss across the batch
    avg_loss = tf.reduce_mean(input_tensor=total_loss)

    # Finally we return the average loss
    return avg_loss, non_finite_files


# Adam Optimization
# =================

# In contrast to 'Deep Speech: Scaling up end-to-end speech recognition'
# (http://arxiv.org/abs/1412.5567),
# in which 'Nesterov's Accelerated Gradient Descent'
# (www.cs.toronto.edu/~fritz/absps/momentum.pdf) was used,
# we will use the Adam method for optimization (http://arxiv.org/abs/1412.6980),
# because, generally, it requires less fine-tuning.
def create_optimizer(learning_rate):
    optimizer = NovoGrad(learning_rate=learning_rate,
                         beta1=FLAGS.beta1,
                         beta2=FLAGS.beta2,
                         epsilon=FLAGS.epsilon,
                         weight_decay=FLAGS.weight_decay)
    return optimizer


# Towers
# ======

# In order to properly make use of multiple GPU's, one must introduce new abstractions,
# not present when using a single GPU, that facilitate the multi-GPU use case.
# In particular, one must introduce a means to isolate the inference and gradient
# calculations on the various GPU's.
# The abstraction we intoduce for this purpose is called a 'tower'.
# A tower is specified by two properties:
# * **Scope** - A scope, as provided by `tf.name_scope()`,
# is a means to isolate the operations within a tower.
# For example, all operations within 'tower 0' could have their name prefixed with `tower_0/`.
# * **Device** - A hardware device, as provided by `tf.device()`,
# on which all operations within the tower execute.
# For example, all operations of 'tower 0' could execute on the first GPU `tf.device('/gpu:0')`.

def get_tower_results(iterator, optimizer, dropout_rates, is_training):
    r'''
    With this preliminary step out of the way, we can for each GPU introduce a
    tower for which's batch we calculate and return the optimization gradients
    and the average loss across towers.
    '''
    # To calculate the mean of the losses
    tower_avg_losses = []

    # Tower gradients to return
    tower_gradients = []

    # Aggregate any non finite files in the batches
    tower_non_finite_files = []

    with tfv1.variable_scope(tfv1.get_variable_scope()):
        # Loop over available_devices
        for i in range(len(Config.available_devices)):
            # Execute operations of tower i on device i
            device = Config.available_devices[i]
            with tf.device(device):
                # Create a scope for all operations of tower i
                with tf.name_scope('tower_%d' % i):
                    # Calculate the avg_loss and mean_edit_distance and retrieve the decoded
                    # batch along with the original batch's labels (Y) of this tower
                    avg_loss, non_finite_files = calculate_mean_edit_distance_and_loss(iterator, dropout_rates, reuse=i > 0, is_training=is_training)

                    # Allow for variables to be re-used by the next tower
                    tfv1.get_variable_scope().reuse_variables()

                    # Retain tower's avg losses
                    tower_avg_losses.append(avg_loss)

                    # Compute gradients for model parameters using tower's mini-batch
                    gradients = optimizer.compute_gradients(avg_loss)

                    # Retain tower's gradients
                    tower_gradients.append(gradients)

                    tower_non_finite_files.append(non_finite_files)

    avg_loss_across_towers = tf.reduce_mean(input_tensor=tower_avg_losses, axis=0)
    tfv1.summary.scalar(name='step_loss', tensor=avg_loss_across_towers, collections=['step_summaries'])

    all_non_finite_files = tf.concat(tower_non_finite_files, axis=0)

    # Return gradients and the average loss
    return tower_gradients, avg_loss_across_towers, all_non_finite_files


def average_gradients(tower_gradients):
    r'''
    A routine for computing each variable's average of the gradients obtained from the GPUs.
    Note also that this code acts as a synchronization point as it requires all
    GPUs to be finished with their mini-batch before it can run to completion.
    '''
    # List of average gradients to return to the caller
    average_grads = []

    # Run this on cpu_device to conserve GPU memory
    with tf.device(Config.cpu_device):
        # Loop over gradient/variable pairs from all towers
        for grad_and_vars in zip(*tower_gradients):
            # Introduce grads to store the gradients for the current variable
            grads = []

            # Loop over the gradients for the current variable
            for g, v in grad_and_vars:
                # Add 0 dimension to the gradients to represent the tower.
                expanded_g = tf.expand_dims(g, 0)
                # Append on a 'tower' dimension which we will average over below.
                grads.append(expanded_g)

            # Average over the 'tower' dimension
            grad = tf.concat(grads, 0)
            grad = tf.reduce_mean(input_tensor=grad, axis=0)

            # Create a gradient/variable tuple for the current variable with its average gradient
            grad_and_var = (grad, grad_and_vars[0][1])

            # Add the current tuple to average_grads
            average_grads.append(grad_and_var)

    # Return result to caller
    return average_grads



# Logging
# =======

def log_variable(variable, gradient=None):
    r'''
    We introduce a function for logging a tensor variable's current state.
    It logs scalar values for the mean, standard deviation, minimum and maximum.
    Furthermore it logs a histogram of its state and (if given) of an optimization gradient.
    '''
    name = variable.name.replace(':', '_')
    mean = tf.reduce_mean(input_tensor=variable)
    tfv1.summary.scalar(name='%s/mean'   % name, tensor=mean)
    tfv1.summary.scalar(name='%s/sttdev' % name, tensor=tf.sqrt(tf.reduce_mean(input_tensor=tf.square(variable - mean))))
    tfv1.summary.scalar(name='%s/max'    % name, tensor=tf.reduce_max(input_tensor=variable))
    tfv1.summary.scalar(name='%s/min'    % name, tensor=tf.reduce_min(input_tensor=variable))
    tfv1.summary.histogram(name=name, values=variable)
    if gradient is not None:
        if isinstance(gradient, tf.IndexedSlices):
            grad_values = gradient.values
        else:
            grad_values = gradient
        if grad_values is not None:
            tfv1.summary.histogram(name='%s/gradients' % name, values=grad_values)


def log_grads_and_vars(grads_and_vars):
    r'''
    Let's also introduce a helper function for logging collections of gradient/variable tuples.
    '''
    for gradient, variable in grads_and_vars:
        log_variable(variable, gradient=gradient)


def train():
    do_cache_dataset = True

    # pylint: disable=too-many-boolean-expressions
    if (FLAGS.data_aug_features_multiplicative > 0 or
            FLAGS.data_aug_features_additive > 0 or
            FLAGS.augmentation_spec_dropout_keeprate < 1 or
            FLAGS.augmentation_freq_and_time_masking or
            FLAGS.augmentation_pitch_and_tempo_scaling or
            FLAGS.augmentation_speed_up_std > 0 or
            FLAGS.augmentation_sparse_warp):
        do_cache_dataset = False

    exception_box = ExceptionBox()

    # Create training and validation datasets
    train_set, train_batches = create_dataset(FLAGS.train_files.split(','),
                                              batch_size=FLAGS.train_batch_size,
                                              enable_cache=FLAGS.feature_cache and do_cache_dataset,
                                              cache_path=FLAGS.feature_cache,
                                              train_phase=True,
                                              exception_box=exception_box,
                                              process_ahead=len(Config.available_devices) * FLAGS.train_batch_size * 2,
                                              buffering=FLAGS.read_buffer)

    iterator = tfv1.data.Iterator.from_structure(tfv1.data.get_output_types(train_set),
                                                 tfv1.data.get_output_shapes(train_set),
                                                 output_classes=tfv1.data.get_output_classes(train_set))

    # Make initialization ops for switching between the two sets
    train_init_op = iterator.make_initializer(train_set)

    if FLAGS.dev_files:
        dev_sources = FLAGS.dev_files.split(',')
        dev_sets = [create_dataset([source],
                                   batch_size=FLAGS.dev_batch_size,
                                   train_phase=False,
                                   exception_box=exception_box,
                                   process_ahead=len(Config.available_devices) * FLAGS.dev_batch_size * 2,
                                   buffering=FLAGS.read_buffer)[0] for source in dev_sources]
        dev_init_ops = [iterator.make_initializer(dev_set) for dev_set in dev_sets]

    # Dropout
    dropout_rates = [tfv1.placeholder(tf.float32, name='dropout_{}'.format(i)) for i in range(6)]
    dropout_feed_dict = {
        dropout_rates[0]: FLAGS.dropout_rate,
        dropout_rates[1]: FLAGS.dropout_rate2,
        dropout_rates[2]: FLAGS.dropout_rate3,
        dropout_rates[3]: FLAGS.dropout_rate4,
        dropout_rates[4]: FLAGS.dropout_rate5,
        dropout_rates[5]: FLAGS.dropout_rate6,
    }
    no_dropout_feed_dict = {
        rate: 0. for rate in dropout_rates
    }

    # global_step is automagically incremented by the optimizer
    global_step = tfv1.train.get_or_create_global_step()

    def linear_warm_up(initial_lr, warm_up):
        return (tf.cast(global_step+1, tf.float32) * initial_lr) / warm_up

    def cosine_annealing(initial_lr, total_steps):
        return tfv1.train.cosine_decay(initial_lr, global_step, total_steps)

    def lr_sched(initial_lr, warm_up, total_steps):
        return tf.cond(global_step < warm_up,
                       lambda: linear_warm_up(initial_lr, warm_up),
                       lambda: cosine_annealing(initial_lr, total_steps),
                       name='learning_rate')

    # Learning rate schedule (warm up + cosine annealing)
    print(f'total batches: {train_batches}, len(devices): {len(Config.available_devices)}, total_epochs: {FLAGS.total_epochs}')
    total_steps = (train_batches // len(Config.available_devices)) * FLAGS.total_epochs
    print(f'total steps: {total_steps}')
    learning_rate = lr_sched(FLAGS.learning_rate, FLAGS.lr_warm_up, total_steps)
    tfv1.summary.scalar(name='learning_rate', tensor=learning_rate, collections=['step_summaries'])

    # Building the graph
    optimizer = create_optimizer(learning_rate)

    # Enable mixed precision training
    if FLAGS.automatic_mixed_precision:
        log_info('Enabling automatic mixed precision training.')
        optimizer = tfv1.train.experimental.enable_mixed_precision_graph_rewrite(optimizer)

    is_training = tfv1.placeholder(tf.bool, name='is_training')
    gradients, loss, non_finite_files = get_tower_results(iterator, optimizer, dropout_rates, is_training)

    # Average tower gradients across GPUs
    avg_tower_gradients = average_gradients(gradients)
    log_grads_and_vars(avg_tower_gradients)

    apply_gradient_op = optimizer.apply_gradients(avg_tower_gradients, global_step=global_step)
    apply_gradient_op = tf.group([apply_gradient_op, tfv1.get_collection(tfv1.GraphKeys.UPDATE_OPS)])

    # Summaries
    step_summaries_op = tfv1.summary.merge_all('step_summaries')
    step_summary_writers = {
        'train': tfv1.summary.FileWriter(os.path.join(FLAGS.summary_dir, 'train'), max_queue=120),
        'dev': tfv1.summary.FileWriter(os.path.join(FLAGS.summary_dir, 'dev'), max_queue=120)
    }

    step_summary_writers['train'].add_graph(tfv1.get_default_graph())

    # Checkpointing
    checkpoint_saver = tfv1.train.Saver(max_to_keep=FLAGS.max_to_keep)
    checkpoint_path = os.path.join(FLAGS.save_checkpoint_dir, 'train')

    best_dev_saver = tfv1.train.Saver(max_to_keep=1)
    best_dev_path = os.path.join(FLAGS.save_checkpoint_dir, 'best_dev')

    # Save flags next to checkpoints
    os.makedirs(FLAGS.save_checkpoint_dir, exist_ok=True)
    flags_file = os.path.join(FLAGS.save_checkpoint_dir, 'flags.txt')
    with open(flags_file, 'w') as fout:
        fout.write(FLAGS.flags_into_string())

    with tfv1.Session(config=Config.session_config) as session:
        log_debug('Session opened.')

        # Prevent further graph changes
        tfv1.get_default_graph().finalize()

        # Load checkpoint or initialize variables
        if FLAGS.load == 'auto':
            method_order = ['best', 'last', 'init']
        else:
            method_order = [FLAGS.load]
        load_or_init_graph(session, method_order)

        def run_set(set_name, epoch, init_op, dataset=None):
            is_train = set_name == 'train'
            train_op = apply_gradient_op if is_train else []
            feed_dict = {is_training: is_train}

            total_loss = 0.0
            step_count = 0

            step_summary_writer = step_summary_writers.get(set_name)
            checkpoint_time = time.time()

            # Setup progress bar
            class LossWidget(progressbar.widgets.FormatLabel):
                def __init__(self):
                    progressbar.widgets.FormatLabel.__init__(self, format='Loss: %(mean_loss)f')

                def __call__(self, progress, data, **kwargs):
                    data['mean_loss'] = total_loss / step_count if step_count else 0.0
                    return progressbar.widgets.FormatLabel.__call__(self, progress, data, **kwargs)

            prefix = 'Epoch {} | {:>10}'.format(epoch, 'Training' if is_train else 'Validation')
            widgets = [' | ', progressbar.widgets.Timer(),
                       ' | Steps: ', progressbar.widgets.Counter(),
                       ' | ', LossWidget()]
            suffix = ' | Dataset: {}'.format(dataset) if dataset else None
            pbar = create_progressbar(prefix=prefix, widgets=widgets, suffix=suffix).start()

            # Initialize iterator to the appropriate dataset
            session.run(init_op)

            # Batch loop
            while True:
                try:
                    _, current_step, batch_loss, problem_files, step_summary = \
                        session.run([train_op, global_step, loss, non_finite_files, step_summaries_op],
                                    feed_dict=feed_dict)
                    exception_box.raise_if_set()
                except tf.errors.InvalidArgumentError as err:
                    if FLAGS.augmentation_sparse_warp:
                        log_info("Ignoring sparse warp error: {}".format(err))
                        continue
                    else:
                        raise
                except tf.errors.OutOfRangeError:
                    exception_box.raise_if_set()
                    break

                if problem_files.size > 0:
                    problem_files = [f.decode('utf8') for f in problem_files[..., 0]]
                    log_error('The following files caused an infinite (or NaN) '
                              'loss: {}'.format(','.join(problem_files)))

                total_loss += batch_loss
                step_count += 1

                pbar.update(step_count)

                step_summary_writer.add_summary(step_summary, current_step)

                if is_train and FLAGS.checkpoint_secs > 0 and time.time() - checkpoint_time > FLAGS.checkpoint_secs:
                    checkpoint_saver.save(session, checkpoint_path, global_step=current_step)
                    checkpoint_time = time.time()

            pbar.finish()
            mean_loss = total_loss / step_count if step_count > 0 else 0.0
            return mean_loss, step_count

        log_info('STARTING Optimization')
        train_start_time = datetime.utcnow()
        best_dev_loss = float('inf')
        dev_losses = []
        epochs_without_improvement = 0
        try:
            for epoch in range(FLAGS.epochs):
                # Training
                log_progress('Training epoch %d...' % epoch)
                train_loss, _ = run_set('train', epoch, train_init_op)
                log_progress('Finished training epoch %d - loss: %f' % (epoch, train_loss))
                checkpoint_saver.save(session, checkpoint_path, global_step=global_step)

                if FLAGS.dev_files:
                    # Validation
                    dev_loss = 0.0
                    total_steps = 0
                    for source, init_op in zip(dev_sources, dev_init_ops):
                        log_progress('Validating epoch %d on %s...' % (epoch, source))
                        set_loss, steps = run_set('dev', epoch, init_op, dataset=source)
                        dev_loss += set_loss * steps
                        total_steps += steps
                        log_progress('Finished validating epoch %d on %s - loss: %f' % (epoch, source, set_loss))

                    dev_loss = dev_loss / total_steps
                    dev_losses.append(dev_loss)

                    # Count epochs without an improvement for early stopping and reduction of learning rate on a plateau
                    # the improvement has to be greater than FLAGS.es_min_delta
                    if dev_loss > best_dev_loss - FLAGS.es_min_delta:
                        epochs_without_improvement += 1
                    else:
                        epochs_without_improvement = 0

                    # Save new best model
                    if dev_loss < best_dev_loss:
                        best_dev_loss = dev_loss
                        save_path = best_dev_saver.save(session, best_dev_path, global_step=global_step, latest_filename='best_dev_checkpoint')
                        log_info("Saved new best validating model with loss %f to: %s" % (best_dev_loss, save_path))

                    # Early stopping
                    if FLAGS.early_stop and epochs_without_improvement == FLAGS.es_epochs:
                        log_info('Early stop triggered as the loss did not improve the last {} epochs'.format(
                            epochs_without_improvement))
                        break

                    # Reduce learning rate on plateau
                    if (FLAGS.reduce_lr_on_plateau and
                            epochs_without_improvement % FLAGS.plateau_epochs == 0 and epochs_without_improvement > 0):
                        # If the learning rate was reduced and there is still no improvement
                        # wait FLAGS.plateau_epochs before the learning rate is reduced again
                        session.run(reduce_learning_rate_op)
                        current_learning_rate = learning_rate_var.eval()
                        log_info('Encountered a plateau, reducing learning rate to {}'.format(
                            current_learning_rate))

        except KeyboardInterrupt:
            pass
        log_info('FINISHED optimization in {}'.format(datetime.utcnow() - train_start_time))
    log_debug('Session closed.')


def test():
    samples = evaluate(FLAGS.test_files.split(','), create_model)
    if FLAGS.test_output_file:
        # Save decoded tuples as JSON, converting NumPy floats to Python floats
        json.dump(samples, open(FLAGS.test_output_file, 'w'), default=float)


def create_inference_graph(batch_size=1, n_steps=16, tflite=False):
    batch_size = batch_size if batch_size > 0 else None

    # Create feature computation graph
    if batch_size > 0:
        input_samples = tfv1.placeholder(tf.float32, [Config.audio_window_samples], 'input_samples')
        samples = tf.expand_dims(input_samples, -1)
        mfccs, _ = samples_to_mfccs(samples, FLAGS.audio_sample_rate)
        mfccs = tf.identity(mfccs, name='mfccs')

    # Input tensor will be of shape [batch_size, n_steps, n_input]
    # This shape is read by the native_client in DS_CreateModel to know the
    # value of n_steps, n_context and n_input. Make sure you update the code
    # there if this shape is changed.
    input_tensor = tfv1.placeholder(tf.float32, [batch_size, n_steps if n_steps > 0 else None, Config.n_input], name='input_node')
    seq_length = tfv1.placeholder(tf.int32, [batch_size], name='input_lengths')

    with tf.variable_scope('model'):
        logits, encoded_lens = create_model(input_tensor,
                                            seq_length,
                                            is_training=False,
                                            n_steps=n_steps if n_steps > 0 else None)

    # TF Lite runtime will check that input dimensions are 1, 2 or 4
    # by default we get 3, the middle one being batch_size which is forced to
    # one on inference graph, so remove that dimension
    if tflite:
        logits = tf.squeeze(logits, [0])

    # Apply softmax for CTC decoder
    logits = tf.nn.softmax(logits, name='logits')

    if batch_size <= 0:
        if tflite:
            raise NotImplementedError('dynamic batch_size does not support tflite nor streaming')
        if n_steps > 0:
            raise NotImplementedError('dynamic batch_size expect n_steps to be dynamic too')
        inputs = {
            'input': input_tensor,
            'input_lengths': seq_length,
        }
        outputs = {
            'outputs': logits,
            'encoded_lengths': encoded_lens,
        }
        return inputs, outputs

    inputs = {
        'input': input_tensor,
        'input_lengths': seq_length,
        'input_samples': input_samples,
    }
    outputs = {
        'outputs': logits,
        'encoded_lengths': encoded_lens,
        'mfccs': mfccs,
    }
    return inputs, outputs



def file_relative_read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


def export():
    r'''
    Restores the trained variables into a simpler graph that will be exported for serving.
    '''
    log_info('Exporting the model...')
    from tensorflow.python.framework.ops import Tensor, Operation

    inputs, outputs = create_inference_graph(batch_size=FLAGS.export_batch_size, n_steps=FLAGS.n_steps, tflite=FLAGS.export_tflite)

    graph_version = int(file_relative_read('GRAPH_VERSION').strip())
    assert graph_version > 0

    outputs['metadata_version'] = tf.constant([graph_version], name='metadata_version')
    outputs['metadata_sample_rate'] = tf.constant([FLAGS.audio_sample_rate], name='metadata_sample_rate')
    outputs['metadata_feature_win_len'] = tf.constant([FLAGS.feature_win_len], name='metadata_feature_win_len')
    outputs['metadata_feature_win_step'] = tf.constant([FLAGS.feature_win_step], name='metadata_feature_win_step')
    outputs['metadata_beam_width'] = tf.constant([FLAGS.export_beam_width], name='metadata_beam_width')
    outputs['metadata_alphabet'] = tf.constant([Config.alphabet.serialize()], name='metadata_alphabet')

    if FLAGS.export_language:
        outputs['metadata_language'] = tf.constant([FLAGS.export_language.encode('utf-8')], name='metadata_language')

    # Prevent further graph changes
    tfv1.get_default_graph().finalize()

    output_names_tensors = [tensor.op.name for tensor in outputs.values() if isinstance(tensor, Tensor)]
    output_names_ops = [op.name for op in outputs.values() if isinstance(op, Operation)]
    output_names = output_names_tensors + output_names_ops

    with tf.Session() as session:
        # Restore variables from checkpoint
        if FLAGS.load == 'auto':
            method_order = ['best', 'last']
        else:
            method_order = [FLAGS.load]
        load_or_init_graph(session, method_order)

        output_filename = FLAGS.export_file_name + '.pb'
        if FLAGS.remove_export:
            if os.path.isdir(FLAGS.export_dir):
                log_info('Removing old export')
                shutil.rmtree(FLAGS.export_dir)

        output_graph_path = os.path.join(FLAGS.export_dir, output_filename)

        if not os.path.isdir(FLAGS.export_dir):
            os.makedirs(FLAGS.export_dir)

        frozen_graph = tfv1.graph_util.convert_variables_to_constants(
            sess=session,
            input_graph_def=tfv1.get_default_graph().as_graph_def(),
            output_node_names=output_names)

        frozen_graph = tfv1.graph_util.extract_sub_graph(
            graph_def=frozen_graph,
            dest_nodes=output_names)

        if not FLAGS.export_tflite:
            with open(output_graph_path, 'wb') as fout:
                fout.write(frozen_graph.SerializeToString())
        else:
            output_tflite_path = os.path.join(FLAGS.export_dir, output_filename.replace('.pb', '.tflite'))

            converter = tf.lite.TFLiteConverter(frozen_graph, input_tensors=inputs.values(), output_tensors=outputs.values())
            converter.optimizations = [tf.lite.Optimize.DEFAULT]
            # AudioSpectrogram and Mfcc ops are custom but have built-in kernels in TFLite
            converter.allow_custom_ops = True
            tflite_model = converter.convert()

            with open(output_tflite_path, 'wb') as fout:
                fout.write(tflite_model)

        log_info('Models exported at %s' % (FLAGS.export_dir))

    metadata_fname = os.path.join(FLAGS.export_dir, '{}_{}_{}.md'.format(
        FLAGS.export_author_id,
        FLAGS.export_model_name,
        FLAGS.export_model_version))

    model_runtime = 'tflite' if FLAGS.export_tflite else 'tensorflow'
    with open(metadata_fname, 'w') as f:
        f.write('---\n')
        f.write('author: {}\n'.format(FLAGS.export_author_id))
        f.write('model_name: {}\n'.format(FLAGS.export_model_name))
        f.write('model_version: {}\n'.format(FLAGS.export_model_version))
        f.write('contact_info: {}\n'.format(FLAGS.export_contact_info))
        f.write('license: {}\n'.format(FLAGS.export_license))
        f.write('language: {}\n'.format(FLAGS.export_language))
        f.write('runtime: {}\n'.format(model_runtime))
        f.write('min_ds_version: {}\n'.format(FLAGS.export_min_ds_version))
        f.write('max_ds_version: {}\n'.format(FLAGS.export_max_ds_version))
        f.write('acoustic_model_url: <replace this with a publicly available URL of the acoustic model>\n')
        f.write('scorer_url: <replace this with a publicly available URL of the scorer, if present>\n')
        f.write('---\n')
        f.write('{}\n'.format(FLAGS.export_description))

    log_info('Model metadata file saved to {}. Before submitting the exported model for publishing make sure all information in the metadata file is correct, and complete the URL fields.'.format(metadata_fname))


def package_zip():
    # --export_dir path/to/export/LANG_CODE/ => path/to/export/LANG_CODE.zip
    export_dir = os.path.join(os.path.abspath(FLAGS.export_dir), '') # Force ending '/'
    zip_filename = os.path.dirname(export_dir)

    shutil.copy(FLAGS.scorer_path, export_dir)

    archive = shutil.make_archive(zip_filename, 'zip', export_dir)
    log_info('Exported packaged model {}'.format(archive))


def do_single_file_inference(input_file_path):
    with tfv1.Session(config=Config.session_config) as session:
        inputs, outputs = create_inference_graph(batch_size=1, n_steps=-1)

        # Restore variables from training checkpoint
        if FLAGS.load == 'auto':
            method_order = ['best', 'last']
        else:
            method_order = [FLAGS.load]
        load_or_init_graph(session, method_order)

        features, features_len = audiofile_to_features(input_file_path)

        # Add batch dimension
        features = tf.expand_dims(features, 0)
        features_len = tf.expand_dims(features_len, 0)

        # Evaluate
        features = features.eval(session=session)
        features_len = features_len.eval(session=session)

        if FLAGS.use_greedy_decoder:
            transposed = tf.transpose(outputs['outputs'], [1, 0, 2])
            [decoded_tensor, *_], _ = tf.nn.ctc_greedy_decoder(transposed, outputs['encoded_lengths'])

            decoded = session.run(decoded_tensor, feed_dict={
                inputs['input']: features,
                inputs['input_lengths']: features_len,
            })

            print(sparse_tensor_value_to_texts(decoded, Config.alphabet)[0])
        else:
            logits, encoded_lens = session.run([outputs['outputs'], outputs['encoded_lengths']], feed_dict={
                inputs['input']: features,
                inputs['input_lengths']: features_len,
            })

            logits = np.squeeze(logits)

            if FLAGS.lm_binary_path:
                scorer = Scorer(FLAGS.lm_alpha, FLAGS.lm_beta,
                                FLAGS.scorer_path, Config.alphabet)
            else:
                scorer = None
            decoded = ctc_beam_search_decoder(logits, Config.alphabet, FLAGS.beam_width,
                                              scorer=scorer, cutoff_prob=FLAGS.cutoff_prob,
                                              cutoff_top_n=FLAGS.cutoff_top_n)
            # Print highest probability result
            print(decoded[0][1])


def main(_):
    initialize_globals()

    if FLAGS.train_files:
        tfv1.reset_default_graph()
        tfv1.set_random_seed(FLAGS.random_seed)
        train()

    if FLAGS.test_files:
        tfv1.reset_default_graph()
        test()

    if FLAGS.export_dir and not FLAGS.export_zip:
        tfv1.reset_default_graph()
        export()

    if FLAGS.export_zip:
        tfv1.reset_default_graph()
        FLAGS.export_tflite = True

        if os.listdir(FLAGS.export_dir):
            log_error('Directory {} is not empty, please fix this.'.format(FLAGS.export_dir))
            sys.exit(1)

        export()
        package_zip()

    if FLAGS.one_shot_infer:
        tfv1.reset_default_graph()
        do_single_file_inference(FLAGS.one_shot_infer)


if __name__ == '__main__':
    create_flags()
    absl.app.run(main)
