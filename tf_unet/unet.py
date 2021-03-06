# tf_unet is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# tf_unet is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with tf_unet.  If not, see <http://www.gnu.org/licenses/>.


'''
Created on Jul 28, 2016

author: jakeret
'''
from __future__ import print_function, division, absolute_import, unicode_literals

import os
import shutil
import numpy as np
from collections import OrderedDict
import logging

import tensorflow as tf

from tf_unet import util
from tf_unet.layers import (weight_variable, weight_variable_devonc, bias_variable,
                            conv2d, deconv2d, max_pool, crop_and_concat, pixel_wise_softmax_2,
                            cross_entropy)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

# this is a simpler version of Tensorflow's 'official' version. See:
# https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/layers/python/layers/layers.py#L102
def get_img_list(data_path):
    l = glob.glob(os.path.join(data_path, '*'))
    l = [f for f in l if re.search("^\d+.mat$", os.path.basename(f))]
    train_list = []
    for f in l:
        if os.path.exists(f):
            if os.path.exists(f[:-4] + '_2.mat'):
                train_list.append([f, f[:-4] + '_2.mat', 2])
            if os.path.exists(f[:-4] + '_3.mat'):
                train_list.append([f, f[:-4] + '_3.mat', 3])
            if os.path.exists(f[:-4] + '_4.mat'):
                train_list.append([f, f[:-4] + '_4.mat', 4])
    return train_list

def get_image_batch(train_list, offset, batch_size):
    target_list = train_list[offset:offset + batch_size]
    input_list = []
    gt_list = []
    cbcr_list = []
    for pair in target_list:
        input_img = scipy.io.loadmat(pair[1])['img_2']
        gt_img = scipy.io.loadmat(pair[0])['img_raw']
        input_list.append(input_img)
        gt_list.append(gt_img)
    input_list = np.array(input_list)
    input_list.resize([batch_size, input_list[0].shape[0],input_list[0].shape[1], 1])
    gt_list = np.array(gt_list)
    gt_list.resize([batch_size, gt_list[0].shape[0],gt_list[0].shape[1], 1])
    return (input_list, gt_list, np.array(cbcr_list))

def get_test_image(test_list, offset, batch_size):
    target_list = train_list[offset:offset + batch_size]
    input_list = []
    gt_list = []
    cbcr_list = []
    for pair in target_list:
        input_img = scipy.io.loadmat(pair[1])['img_2']
        gt_img = scipy.io.loadmat(pair[0])['img_raw']
        input_list.append(input_img)
        gt_list.append(gt_img)
    input_list = np.array(input_list)
    input_list.resize([batch_size, input_list[0].shape[0],input_list[0].shape[1], 1])
    gt_list = np.array(gt_list)
    gt_list.resize([batch_size, gt_list[0].shape[0],gt_list[0].shape[1], 1])
    return (input_list, gt_list, np.array(cbcr_list))

def batch_norm_wrapper(inputs, is_training, decay=0.999):

    epsilon = 1e-3
    scale = tf.Variable(tf.ones([inputs.get_shape()[-1]]))
    beta = tf.Variable(tf.zeros([inputs.get_shape()[-1]]))
    pop_mean = tf.Variable(tf.zeros([inputs.get_shape()[-1]]),
                           trainable=False)
    pop_var = tf.Variable(tf.ones([inputs.get_shape()[-1]]),
                          trainable=False)

    if is_training:
        (batch_mean, batch_var) = tf.nn.moments(inputs, [0, 1, 2])

        # Small epsilon value for the BN transform



        # print(batch_mean.get_shape())
        # print(pop_mean.get_shape())

        train_mean = tf.assign(pop_mean, pop_mean * decay + batch_mean
                               * (1 - decay))
        train_var = tf.assign(pop_var, pop_var * decay + batch_var * (1
                              - decay))
        with tf.control_dependencies([train_mean, train_var]):
            return tf.nn.batch_normalization(
                inputs,
                batch_mean,
                batch_var,
                beta,
                scale,
                epsilon,
                )
    else:
        return tf.nn.batch_normalization(
            inputs,
            pop_mean,
            pop_var,
            beta,
            scale,
            epsilon,
            )


def unet(
    x,
    is_training,
    keep_prob=1,
    channels=1,
    n_class=1,
    layers=3,
    features_root=64,
    filter_size=3,
    pool_size=2,
    summaries=False,
    ):
    """
    Creates a new convolutional unet for the given parametrization.

    :param x: input tensor, shape [?,nx,ny,channels]
    :param keep_prob: dropout probability tensor
    :param channels: number of channels in the input image
    :param n_class: number of output labels
    :param layers: number of layers in the net
    :param features_root: number of features in the first layer
    :param filter_size: size of the convolution filter
    :param pool_size: size of the max pooling operation
    :param summaries: Flag if summaries should be created
    """

    with tf.device('/gpu:0'):
        logging.info('Layers {layers}, features {features}, filter size {filter_size}x{filter_size}, pool size: {pool_size}x{pool_size}'.format(layers=layers,
                     features=features_root, filter_size=filter_size,
                     pool_size=pool_size))

        # Placeholder for the input image

        nx = tf.shape(x)[1]
        ny = tf.shape(x)[2]
        x_image = tf.reshape(x, tf.stack([-1, nx, ny, channels]))
        in_node = x_image
        batch_size = tf.shape(x_image)[0]

        weights = []
        biases = []
        convs = []
        pools = OrderedDict()
        deconv = OrderedDict()
        dw_h_convs = OrderedDict()
        up_h_convs = OrderedDict()

        in_size = 1000
        size = in_size

        # down layers

        for layer in range(0, layers):
            features = 2 ** layer * features_root
            stddev = np.sqrt(2 / (filter_size ** 2 * features))
            if layer == 0:
                w1 = tf.get_variable('down_conv_00_w1', [filter_size,
                        filter_size, channels, features],
                        initializer=tf.random_normal_initializer(stddev=stddev))
            else:
                w1 = tf.get_variable('down_conv_%02d_w1' % (layer + 1),
                        [filter_size, filter_size, features // 2,
                        features],
                        initializer=tf.random_normal_initializer(stddev=stddev))
            w2 = tf.get_variable('down_conv_%02d_w2' % (layer + 1),
                                 [filter_size, filter_size, features,
                                 features],
                                 initializer=tf.random_normal_initializer(stddev=stddev))
            b1 = tf.get_variable('conv_%02d_b1' % (layer + 1),
                                 [features],
                                 initializer=tf.constant_initializer(0.1))
            b2 = tf.get_variable('conv_%02d_b2' % (layer + 1),
                                 [features],
                                 initializer=tf.constant_initializer(0.1))
            conv1 = conv2d(in_node, w1, keep_prob)
            print(conv1.get_shape())
            conv1 = batch_norm_wrapper(conv1, is_training)
            tmp_h_conv = tf.nn.relu(conv1 + b1)
            conv2 = conv2d(tmp_h_conv, w2, keep_prob)
            conv2 = batch_norm_wrapper(conv2, is_training)
            dw_h_convs[layer] = tf.nn.relu(conv2 + b2)
            weights.append((w1, w2))
            biases.append((b1, b2))
            convs.append((conv1, conv2))
            if layer < layers - 1:
                pools[layer] = max_pool(dw_h_convs[layer], pool_size)
                in_node = pools[layer]
        in_node = dw_h_convs[layers - 1]

            # up layers

        for layer in range(layers - 2, -1, -1):
            features = 2 ** (layer + 1) * features_root
            stddev = np.sqrt(2 / (filter_size ** 2 * features))

             # wd = weight_variable_devonc([pool_size, pool_size, features//2, features], stddev)

            wd = tf.get_variable('up_conv_%02d_wd' % (layer + 1),
                                 [pool_size, pool_size, features // 2,
                                 features],
                                 initializer=tf.random_normal_initializer(stddev=stddev))

            # bd = bias_variable([features//2])

            bd = tf.get_variable('up_conv_%02d_bd' % (layer + 1),
                                 [features // 2],
                                 initializer=tf.constant_initializer(0.1))
            h_deconv = tf.nn.relu(deconv2d(in_node, wd, pool_size) + bd)
            h_deconv_concat = crop_and_concat(dw_h_convs[layer],
                    h_deconv)
            deconv[layer] = h_deconv_concat

            # w1 = weight_variable([filter_size, filter_size, features, features//2], stddev)

            w1 = tf.get_variable('up_conv_%02d_w1' % (layer + 1),
                                 [filter_size, filter_size, features,
                                 features // 2],
                                 initializer=tf.random_normal_initializer(stddev=stddev))

            # w2 = weight_variable([filter_size, filter_size, features//2, features//2], stddev)

            w2 = tf.get_variable('up_conv_%02d_w2' % (layer + 1),
                                 [filter_size, filter_size, features
                                 // 2, features // 2],
                                 initializer=tf.random_normal_initializer(stddev=stddev))

            # b1 = bias_variable([features//2])

            b1 = tf.get_variable('up_conv_%02d_b1' % (layer + 1),
                                 [features // 2],
                                 initializer=tf.constant_initializer(0.1))

            # b2 = bias_variable([features//2])

            b2 = tf.get_variable('up_conv_%02d_b2' % (layer + 1),
                                 [features // 2],
                                 initializer=tf.constant_initializer(0.1))

            conv1 = conv2d(h_deconv_concat, w1, keep_prob)
            conv1 = batch_norm_wrapper(conv1, is_training)
            h_conv = tf.nn.relu(conv1 + b1)
            conv2 = conv2d(h_conv, w2, keep_prob)
            conv2 = batch_norm_wrapper(conv2, is_training)
            in_node = tf.nn.relu(conv2 + b2)
            up_h_convs[layer] = in_node

            weights.append((w1, w2))
            biases.append((b1, b2))
            convs.append((conv1, conv2))

            # size *= 2
            # size -= 4

        # Output Map
        # weight = weight_variable([1, 1, features_root, n_class], stddev)

        weight = tf.get_variable('weight', [1, 1, features_root,
                                 n_class],
                                 initializer=tf.random_normal_initializer(stddev=stddev))

        # bias = bias_variable([n_class])

        bias = tf.get_variable('bias', [n_class],
                               initializer=tf.constant_initializer(0.1))
        conv = conv2d(in_node, weight, tf.constant(1.0))

            # conv = batch_norm_wrapper(conv, is_training)

        output_map = tf.nn.relu(conv + bias)

        # output_map = tf.add(output_map, x_image)

        up_h_convs['out'] = output_map

        if summaries:
            for (i, (c1, c2)) in enumerate(convs):
                tf.summary.image('summary_conv_%02d_01' % i,
                                 get_image_summary(c1))
                tf.summary.image('summary_conv_%02d_02' % i,
                                 get_image_summary(c2))

            for k in pools.keys():
                tf.summary.image('summary_pool_%02d' % k,
                                 get_image_summary(pools[k]))

            for k in deconv.keys():
                tf.summary.image('summary_deconv_concat_%02d' % k,
                                 get_image_summary(deconv[k]))

            for k in dw_h_convs.keys():
                tf.summary.histogram('dw_convolution_%02d' % k
                        + '/activations', dw_h_convs[k])

            for k in up_h_convs.keys():
                tf.summary.histogram('up_convolution_%s' % k
                        + '/activations', up_h_convs[k])

        variables = []
        for (w1, w2) in weights:
            variables.append(w1)
            variables.append(w2)

        for (b1, b2) in biases:
            variables.append(b1)
            variables.append(b2)

        # return output_map, variables, int(in_size - size)

            return (output_map, variables)

def model(input_tensor):
	with tf.device("/gpu:0"):
		weights = []
		tensor = None

		#conv_00_w = tf.get_variable("conv_00_w", [3,3,1,64], initializer=tf.contrib.layers.xavier_initializer())
		conv_00_w = tf.get_variable("conv_00_w", [3,3,1,64], initializer=tf.random_normal_initializer(stddev=np.sqrt(2.0/9)))
		conv_00_b = tf.get_variable("conv_00_b", [64], initializer=tf.constant_initializer(0))
		weights.append(conv_00_w)
		weights.append(conv_00_b)
		tensor = tf.nn.relu(tf.nn.bias_add(tf.nn.conv2d(input_tensor, conv_00_w, strides=[1,1,1,1], padding='SAME'), conv_00_b))

		for i in range(18):
			#conv_w = tf.get_variable("conv_%02d_w" % (i+1), [3,3,64,64], initializer=tf.contrib.layers.xavier_initializer())
			conv_w = tf.get_variable("conv_%02d_w" % (i+1), [3,3,64,64], initializer=tf.random_normal_initializer(stddev=np.sqrt(2.0/9/64)))
			conv_b = tf.get_variable("conv_%02d_b" % (i+1), [64], initializer=tf.constant_initializer(0))
			weights.append(conv_w)
			weights.append(conv_b)
			tensor = tf.nn.relu(tf.nn.bias_add(tf.nn.conv2d(tensor, conv_w, strides=[1,1,1,1], padding='SAME'), conv_b))

		#conv_w = tf.get_variable("conv_19_w", [3,3,64,1], initializer=tf.contrib.layers.xavier_initializer())
		conv_w = tf.get_variable("conv_20_w", [3,3,64,1], initializer=tf.random_normal_initializer(stddev=np.sqrt(2.0/9/64)))
		conv_b = tf.get_variable("conv_20_b", [1], initializer=tf.constant_initializer(0))
		weights.append(conv_w)
		weights.append(conv_b)
		tensor = tf.nn.bias_add(tf.nn.conv2d(tensor, conv_w, strides=[1,1,1,1], padding='SAME'), conv_b)

		# tensor = tf.add(tensor, input_tensor)
		return tensor, weights


def create_conv_net(x, keep_prob, channels, n_class, layers=3, features_root=16, filter_size=3, pool_size=2, summaries=True):
    """
    Creates a new convolutional unet for the given parametrization.

    :param x: input tensor, shape [?,nx,ny,channels]
    :param keep_prob: dropout probability tensor
    :param channels: number of channels in the input image
    :param n_class: number of output labels
    :param layers: number of layers in the net
    :param features_root: number of features in the first layer
    :param filter_size: size of the convolution filter
    :param pool_size: size of the max pooling operation
    :param summaries: Flag if summaries should be created
    """

    logging.info("Layers {layers}, features {features}, filter size {filter_size}x{filter_size}, pool size: {pool_size}x{pool_size}".format(layers=layers,
                                                                                                           features=features_root,
                                                                                                           filter_size=filter_size,
                                                                                                           pool_size=pool_size))
    # Placeholder for the input image
    nx = tf.shape(x)[1]
    ny = tf.shape(x)[2]
    x_image = tf.reshape(x, tf.stack([-1,nx,ny,channels]))
    in_node = x_image
    batch_size = tf.shape(x_image)[0]

    weights = []
    biases = []
    convs = []
    pools = OrderedDict()
    deconv = OrderedDict()
    dw_h_convs = OrderedDict()
    up_h_convs = OrderedDict()

    in_size = 1000
    size = in_size
    # down layers
    for layer in range(0, layers):
        features = 2**layer*features_root
        stddev = np.sqrt(2 / (filter_size**2 * features))
        if layer == 0:
            w1 = weight_variable([filter_size, filter_size, channels, features], stddev)
        else:
            w1 = weight_variable([filter_size, filter_size, features//2, features], stddev)

        w2 = weight_variable([filter_size, filter_size, features, features], stddev)
        b1 = bias_variable([features])
        b2 = bias_variable([features])

        conv1 = conv2d(in_node, w1, keep_prob)
        tmp_h_conv = tf.nn.relu(conv1 + b1)
        conv2 = conv2d(tmp_h_conv, w2, keep_prob)
        dw_h_convs[layer] = tf.nn.relu(conv2 + b2)

        weights.append((w1, w2))
        biases.append((b1, b2))
        convs.append((conv1, conv2))

        # size -= 4
        if layer < layers-1:
            pools[layer] = max_pool(dw_h_convs[layer], pool_size)
            in_node = pools[layer]
            # size /= 2

    in_node = dw_h_convs[layers-1]

    # up layers
    for layer in range(layers-2, -1, -1):
        features = 2**(layer+1)*features_root
        stddev = np.sqrt(2 / (filter_size**2 * features))

        wd = weight_variable_devonc([pool_size, pool_size, features//2, features], stddev)
        bd = bias_variable([features//2])
        h_deconv = tf.nn.relu(deconv2d(in_node, wd, pool_size) + bd)
        h_deconv_concat = crop_and_concat(dw_h_convs[layer], h_deconv)
        deconv[layer] = h_deconv_concat

        w1 = weight_variable([filter_size, filter_size, features, features//2], stddev)
        w2 = weight_variable([filter_size, filter_size, features//2, features//2], stddev)
        b1 = bias_variable([features//2])
        b2 = bias_variable([features//2])

        conv1 = conv2d(h_deconv_concat, w1, keep_prob)
        h_conv = tf.nn.relu(conv1 + b1)
        conv2 = conv2d(h_conv, w2, keep_prob)
        in_node = tf.nn.relu(conv2 + b2)
        up_h_convs[layer] = in_node

        weights.append((w1, w2))
        biases.append((b1, b2))
        convs.append((conv1, conv2))

        # size *= 2
        # size -= 4

    # Output Map
    weight = weight_variable([1, 1, features_root, n_class], stddev)
    bias = bias_variable([n_class])
    conv = conv2d(in_node, weight, tf.constant(1.0))
    output_map = tf.nn.relu(conv + bias)
    output_map = tf.add(output_map, x_image)
    up_h_convs["out"] = output_map

    if summaries:
        for i, (c1, c2) in enumerate(convs):
            tf.summary.image('summary_conv_%02d_01'%i, get_image_summary(c1))
            tf.summary.image('summary_conv_%02d_02'%i, get_image_summary(c2))

        for k in pools.keys():
            tf.summary.image('summary_pool_%02d'%k, get_image_summary(pools[k]))

        for k in deconv.keys():
            tf.summary.image('summary_deconv_concat_%02d'%k, get_image_summary(deconv[k]))

        for k in dw_h_convs.keys():
            tf.summary.histogram("dw_convolution_%02d"%k + '/activations', dw_h_convs[k])

        for k in up_h_convs.keys():
            tf.summary.histogram("up_convolution_%s"%k + '/activations', up_h_convs[k])

    variables = []
    for w1,w2 in weights:
        variables.append(w1)
        variables.append(w2)

    for b1,b2 in biases:
        variables.append(b1)
        variables.append(b2)


    return output_map, variables, int(in_size - size)


class Unet(object):
    """
    A unet implementation

    :param channels: (optional) number of channels in the input image
    :param n_class: (optional) number of output labels
    :param cost: (optional) name of the cost function. Default is 'cross_entropy'
    :param cost_kwargs: (optional) kwargs passed to the cost function. See Unet._get_cost for more options
    """

    def __init__(self, channels=3, n_class=2, cost="cross_entropy", cost_kwargs={}, **kwargs):
        tf.reset_default_graph()

        self.n_class = n_class
        self.summaries = kwargs.get("summaries", False)
        self.global_step = tf.Variable(0, trainable=False)

        self.x = tf.placeholder("float", shape=[None, None, None, channels])
        self.y = tf.placeholder("float", shape=[None, None, None, n_class])
        self.keep_prob = tf.placeholder(tf.float32) #dropout (keep probability)

        # logits, self.variables, self.offset = create_conv_net(self.x, self.keep_prob, channels, n_class, **kwargs)
        logits, self.variables = unet(self.x, self.keep_prob, channels, n_class, **kwargs)

        # logits, self.variables = model(self.x)

        # self.cost = self._get_cost(logits, cost, cost_kwargs)
        self.predicter = logits
        self.cost = tf.reduce_mean(tf.nn.l2_loss(tf.subtract(logits,self.y)))
        self.correct_pred = tf.equal(self.predicter, self.y)
        self.accuracy = tf.reduce_mean(tf.cast(self.correct_pred, tf.float32))
        self.optimizer = tf.train.AdamOptimizer(learning_rate)  # tf.train.MomentumOptimizer(learning_rate, 0.9)
        self.opt = optimizer.minimize(self.cost, global_step=global_step)

    def predict(self, model_path, x_test):
        """
        Uses the model to create a prediction for the given data

        :param model_path: path to the model checkpoint to restore
        :param x_test: Data to predict on. Shape [n, nx, ny, channels]
        :returns prediction: The unet prediction Shape [n, px, py, labels] (px=nx-self.offset/2)
        """

        init = tf.global_variables_initializer()
        with tf.Session() as sess:
            # Initialize variables
            sess.run(init)

            # Restore model weights from previously saved model
            self.restore(sess, model_path)

            y_dummy = np.empty((x_test.shape[0], x_test.shape[1], x_test.shape[2], self.n_class))
            prediction = sess.run(self.predicter, feed_dict={self.x: x_test, self.y: y_dummy, self.keep_prob: 1.})

        return prediction

    def save(self, sess, model_path):
        """
        Saves the current session to a checkpoint

        :param sess: current session
        :param model_path: path to file system location
        """

        saver = tf.train.Saver()
        save_path = saver.save(sess, model_path)
        return save_path

    def restore(self, sess, model_path):
        """
        Restores a session from a checkpoint

        :param sess: current session instance
        :param model_path: path to file system checkpoint location
        """

        saver = tf.train.Saver()
        saver.restore(sess, model_path)
        logging.info("Model restored from file: %s" % model_path)

class Trainer(object):
    """
    Trains a unet instance

    :param net: the unet instance to train
    :param batch_size: size of training batch
    :param optimizer: (optional) name of the optimizer to use (momentum or adam)
    :param opt_kwargs: (optional) kwargs passed to the learning rate (momentum opt) and to the optimizer
    """

    prediction_path = "prediction_unet"
    verification_batch_size = 4

    def __init__(self, net, batch_size=1, optimizer="momentum", learning_rate=0.001):
        self.net = net
        self.batch_size = batch_size
        self.opt_kwargs = opt_kwargs
        self.learning_rate = learning_rate


    def _initialize(self, training_iters, output_path, restore):
        global_step = tf.Variable(0)

        # self.norm_gradients_node = tf.Variable(tf.constant(0.0, shape=[len(self.net.gradients_node)]))

        # if self.net.summaries:
        #     tf.summary.histogram('norm_grads', self.norm_gradients_node)

        tf.summary.scalar('loss', self.net.cost)
        tf.summary.scalar('accuracy', self.net.accuracy)

        self.summary_op = tf.summary.merge_all()
        init = tf.global_variables_initializer()

        prediction_path = os.path.abspath(self.prediction_path)
        output_path = os.path.abspath(output_path)

        if not restore:
            logging.info("Removing '{:}'".format(prediction_path))
            shutil.rmtree(prediction_path, ignore_errors=True)
            logging.info("Removing '{:}'".format(output_path))
            shutil.rmtree(output_path, ignore_errors=True)

        if not os.path.exists(prediction_path):
            logging.info("Allocating '{:}'".format(prediction_path))
            os.makedirs(prediction_path)

        if not os.path.exists(output_path):
            logging.info("Allocating '{:}'".format(output_path))
            os.makedirs(output_path)

        return init

    def train(self, data_path, output_path, training_iters=10, epochs=100, dropout=0.75, display_step=1, restore=False):
        """
        Lauches the training process

        :param data_provider: callable returning training and verification data
        :param output_path: path where to store checkpoints
        :param training_iters: number of training mini batch iteration
        :param epochs: number of epochs
        :param dropout: dropout probability
        :param display_step: number of steps till outputting stats
        :param restore: Flag if previous model should be restored
        """
        save_path = os.path.join(output_path, "model.cpkt")
        if epochs == 0:
            return save_path

        init = self._initialize(training_iters, output_path, restore)

        with tf.Session() as sess:
            sess.run(init)

            if restore:
                ckpt = tf.train.get_checkpoint_state(output_path)
                if ckpt and ckpt.model_checkpoint_path:
                    self.net.restore(sess, ckpt.model_checkpoint_path)

            # test_x, test_y = data_provider(self.verification_batch_size)
            train_list = get_train_list(data_path)
            shuffle(train_list)
            pred_shape = self.store_prediction(sess, test_x, test_y, "_init")

            summary_writer = tf.summary.FileWriter(output_path, graph=sess.graph)
            logging.info("Start optimization")

            avg_gradients = None
            for epoch in range(epochs):
                total_loss = 0
                for step in range((epoch*training_iters), ((epoch+1)*training_iters)):
                    batch_x, batch_y = data_provider(self.batch_size)

                    # Run optimization op (backprop)
                    _, loss, lr = sess.run((self.optimizer, self.net.cost, self.learning_rate_node),
                                                      feed_dict={self.net.x: batch_x,
                                                                 self.net.y: util.crop_to_shape(batch_y, pred_shape),
                                                                 self.net.keep_prob: dropout})

                    if avg_gradients is None:
                        avg_gradients = [np.zeros_like(gradient) for gradient in gradients]
                    for i in range(len(gradients)):
                        avg_gradients[i] = (avg_gradients[i] * (1.0 - (1.0 / (step+1)))) + (gradients[i] / (step+1))

                    norm_gradients = [np.linalg.norm(gradient) for gradient in avg_gradients]
                    # self.norm_gradients_node.assign(norm_gradients).eval()

                    if step % display_step == 0:
                        self.output_minibatch_stats(sess, summary_writer, step, batch_x, util.crop_to_shape(batch_y, pred_shape))

                    total_loss += loss

                self.output_epoch_stats(epoch, total_loss, training_iters, lr)
                self.store_prediction(sess, test_x, test_y, "epoch_%s"%epoch)

                save_path = self.net.save(sess, save_path)
            logging.info("Optimization Finished!")

            return save_path

    def store_prediction(self, sess, batch_x, batch_y, name):
        prediction = sess.run(self.net.predicter, feed_dict={self.net.x: batch_x,
                                                             self.net.y: batch_y,
                                                             self.net.keep_prob: 1.})
        pred_shape = prediction.shape

        loss = sess.run(self.net.cost, feed_dict={self.net.x: batch_x,
                                                       self.net.y: util.crop_to_shape(batch_y, pred_shape),
                                                       self.net.keep_prob: 1.})

        logging.info("Verification error= {:.1f}%, loss= {:.4f}".format(error_rate(prediction,
                                                                          util.crop_to_shape(batch_y,
                                                                                             prediction.shape)),
                                                                          loss))

        img = util.combine_img_prediction(batch_x, batch_y, prediction)
        util.save_image(img, "%s/%s.jpg"%(self.prediction_path, name))

        return pred_shape

    def output_epoch_stats(self, epoch, total_loss, training_iters, lr):
        logging.info("Epoch {:}, Average loss: {:.4f}, learning rate: {:.4f}".format(epoch, (total_loss / training_iters), lr))

    def output_minibatch_stats(self, sess, summary_writer, step, batch_x, batch_y):
        # Calculate batch loss and accuracy
        summary_str, loss, acc, predictions = sess.run([self.summary_op,
                                                            self.net.cost,
                                                            self.net.accuracy,
                                                            self.net.predicter],
                                                           feed_dict={self.net.x: batch_x,
                                                                      self.net.y: batch_y,
                                                                      self.net.keep_prob: 1.})
        summary_writer.add_summary(summary_str, step)
        summary_writer.flush()
        logging.info("Iter {:}, Minibatch Loss= {:.4f}, Training Accuracy= {:.4f}, Minibatch error= {:.1f}%".format(step,
                                                                                                            loss,
                                                                                                            acc,
                                                                                                            error_rate(predictions, batch_y)))


def error_rate(predictions, labels):
    """
    Return the error rate based on dense predictions and 1-hot labels.
    """
    return 100.0 - (
        100.0 *
        np.sum(predictions == labels) /
        (predictions.shape[0]*predictions.shape[1]*predictions.shape[2]))

    # return 100.0 - (
    #     100.0 *
    #     np.sum(np.argmax(predictions, 3) == np.argmax(labels, 3)) /
    #     (predictions.shape[0]*predictions.shape[1]*predictions.shape[2]))




def get_image_summary(img, idx=0):
    """
    Make an image summary for 4d tensor image with index idx
    """

    V = tf.slice(img, (0, 0, 0, idx), (1, -1, -1, 1))
    V -= tf.reduce_min(V)
    V /= tf.reduce_max(V)
    V *= 255

    img_w = tf.shape(img)[1]
    img_h = tf.shape(img)[2]
    V = tf.reshape(V, tf.stack((img_w, img_h, 1)))
    V = tf.transpose(V, (2, 0, 1))
    V = tf.reshape(V, tf.stack((-1, img_w, img_h, 1)))
    return V
