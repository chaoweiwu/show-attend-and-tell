# =========================================================================================
# Implementation of "Show, Attend and Tell: Neural Caption Generator With Visual Attention".
# There are some notations.
# N is batch size.
# L is spacial size of feature vector (196).
# D is dimension of image feature vector (512).
# T is the number of time step which is equal to caption's length-1 (16).
# V is vocabulary size (about 10000).
# M is dimension of word vector which is embedding size (default is 512).
# H is dimension of hidden state (default is 1024).
# =========================================================================================
from __future__ import division


import tensorflow as tf
import numpy as np
from layers import init_lstm, word_embedding_forward, project_feature
from layers import attention_forward, rnn_step_forward, lstm_step_forward
from layers import affine_forward, affine_sigmoid_forward, softmax_loss 
from utils import init_weight, init_bias


class CaptionGenerator(object):
    """
    CaptionGenerator produces functions:
        - build_model: receives features and captions then build graph where root nodes are loss and logits.  
        - build_sampler: receives features and build graph where root nodes are captions and alpha weights.
    """
    def __init__(self, word_to_idx, dim_feature=[196, 512], dim_embed=512, dim_hidden=1024, n_time_step=None, cell_type='rnn', 
                prev2out=True, ctx2out=True, alpha_c=0.0, selector=True, use_dropout=True):

        if cell_type not in {'rnn', 'lstm'}:
            raise ValueError('Invalid cell_type "%s"' % cell_type)

        # Initialize some hyper parameters
        self.cell_type = cell_type
        self.word_to_idx = word_to_idx
        self.idx_to_word = {i: w for w, i in word_to_idx.iteritems()}
        self.prev2out = prev2out
        self.ctx2out = ctx2out
        self.alpha_c = alpha_c
        self.selector = selector
        self.use_dropout = use_dropout
        self.V = len(word_to_idx)
        self.L = dim_feature[0]
        self.D = dim_feature[1]
        self.M = dim_embed
        self.H = dim_hidden
        self.T = n_time_step
        self._null = word_to_idx['<NULL>']
        self._start = word_to_idx.get('<START>', None)

        with tf.device('/cpu:0'):
            # Initialize word embedding matrix
            self.W_embed = tf.Variable(tf.random_uniform([self.V, self.M], -1.0, 1.0), name='Wemb', dtype=tf.float32)
            
        # Initialize weights for generating initial hidden and cell states
        self.W1_init_h = init_weight('W1_init_h', [self.D, self.H])
        self.b1_init_h = init_bias('b1_init_h', [self.H])
        self.W2_init_h = init_weight('W2_init_h', [self.H, self.H])
        self.b2_init_h = init_bias('b2_init_h', [self.H])
        self.W1_init_c = init_weight('W1_init_c', [self.D, self.H])
        self.b1_init_c = init_bias('b1_init_c', [self.H])
        self.W2_init_c = init_weight('W2_init_c', [self.H, self.H])
        self.b2_init_c = init_bias('b2_init_c', [self.H])

        # Initialize weights for attention layer 
        self.W_proj_x = init_weight('W_proj_x', [self.D, self.D])
        self.W_proj_h = init_weight('W_proj_h', [self.H, self.D])
        self.b_proj = init_bias('b_proj', [self.D])
        self.W_att = init_weight('W_att', [self.D, 1])

        # Initialize weights for the RNN/LSTM
        dim_mul = {'rnn': 1, 'lstm': 4}[cell_type]
        dim_in = self.M + self.H + self.D
        self.Wx = init_weight('Wx', [self.M, self.H * dim_mul], dim_in=dim_in)    
        self.Wh = init_weight('Wx', [self.H, self.H * dim_mul], dim_in=dim_in)
        self.Wz = init_weight('Wz', [self.D, self.H * dim_mul], dim_in=dim_in)
        self.b = init_bias('b', [self.H * dim_mul])

        # context-to-out
        if self.ctx2out:
            self.W_ctx2out = init_weight('W_ctx2out', [self.D, self.M])
            
        # gating scalar beta
        if self.selector:
            self.W_sel = init_weight('W_sel', [self.H, 1]) 
            self.b_sel = init_bias('b_sel', [1])
        
        # Initialize weights for decode RNN/LSTM hidden state to vocab-size output vector
        self.W1_decode =  init_weight('W1_decode', [self.H, self.M])
        self.b1_decode =  init_bias('b1_decode', [self.M])
        self.W2_decode = init_weight('W2_decode', [self.M, self.V])
        self.b2_decode = init_bias('b2_decode', [self.V])
            
        # Place holder for features and captions
        self.features = tf.placeholder(tf.float32, [None, self.L, self.D])
        self.captions = tf.placeholder(tf.int32, [None, self.T + 1])


    def build_model(self):
        """
        Input:
        - features: input image features of shape (N, L, D)
        - captions: ground-truth captions; an integer array of shape (N, T+1) where each element is in the range [0, V)
        Returns:
        - loss: scalar giving loss
        """

        # for simplification
        features = self.features
        captions = self.captions
        batch_size = tf.shape(features)[0]

        # captions for input/output and mask matrix
        captions_in = captions[:, :self.T]      
        captions_out = captions[:, 1:]  
        mask = tf.not_equal(captions_out, self._null)
        
        # generate initial hidden state using CNN features 
        mean_features = tf.reduce_mean(features, 1)   # (N, D)
        prev_h = init_lstm(mean_features, self.W1_init_h, self.b1_init_h, self.W2_init_h, self.b2_init_h, self.use_dropout)     # (N, H)
        prev_c = init_lstm(mean_features, self.W1_init_c, self.b1_init_c, self.W2_init_c, self.b2_init_c, self.use_dropout)     # (N, H)

        # generate word vector
        x = word_embedding_forward(captions_in, self.W_embed)    # (N, T, M)

        # project feature vector
        features_projected = project_feature(features, self.W_proj_x)    # (N, L, D)

        loss = 0.0
        alpha_list = []
        for t in range(self.T):
            # generate context vector 
            context, alpha = attention_forward(features, features_projected, prev_h, self.W_proj_h, self.b_proj, self.W_att)     # (N, D), (N, L)
            alpha_list.append(alpha)

            if self.selector:
                beta = affine_sigmoid_forward(prev_h, self.W_sel, self.b_sel)    # (N, 1)
                context = beta * context    

            # rnn/lstm forward prop
            if self.cell_type == 'rnn':
                next_h = rnn_step_forward(x[:,t,:], prev_h, context, self.Wx, self.Wh, self.Wz, self.b)     # (N, H)
                prev_h = next_h
            elif self.cell_type == 'lstm': 
                next_h, next_c = lstm_step_forward(x[:,t,:], prev_h, prev_c, context, self.Wx, self.Wh, self.Wz, self.b)     # (N, H), (N, H)
                prev_h = next_h
                prev_c = next_c

            if self.use_dropout:
                next_h = tf.nn.dropout(next_h, 0.5)

            # hidden-to-embed, embed-to-vocab
            logits = affine_forward(next_h, self.W1_decode, self.b1_decode)   # (N, M)
            if self.prev2out:
                logits += x[:,t,:]
            if self.ctx2out:
                logits += tf.matmul(context, self.W_ctx2out)

            logits_h = tf.nn.tanh(logits)
            logits_h = tf.nn.dropout(logits_h, 0.5)
            logits_out = affine_forward(logits_h, self.W2_decode, self.b2_decode)   # (N, V)
       
            # compute softmax loss
            loss += softmax_loss(logits_out, captions_out[:, t], mask[:, t])

        # doubly stochastic regularization
        if self.alpha_c > 0:
            alphas = tf.transpose(tf.pack(alpha_list), (1, 0, 2))     # (N, T, L)
            alphas_all = tf.reduce_sum(alphas, 1)      # (N, L)
            alpha_reg = self.alpha_c * tf.reduce_sum((16./196 - alphas_all) ** 2)     
            loss += alpha_reg  

        return loss / tf.to_float(batch_size)


    def build_sampler(self, max_len=20):
        """
        Input:
        - features: input image features of shape (N, L, D)
        - max_len: max length for generating cations
        
        Returns
        - alphas: soft attention weights for visualization
        - sampled_captions: sampled word indices
        """

        # for simplification
        features = self.features
        
        # generate initial hidden state using CNN features 
        mean_features = tf.reduce_mean(features, 1)   # (N, D)
        prev_h = init_lstm(mean_features, self.W1_init_h, self.b1_init_h, self.W2_init_h, self.b2_init_h)
        prev_c = init_lstm(mean_features, self.W1_init_c, self.b1_init_c, self.W2_init_c, self.b2_init_c)

        # project feature vector
        features_projected = project_feature(features, self.W_proj_x)    # (N, L, D)

        sampled_word_list = []
        alpha_list = []
        for t in range(max_len):
            # embed the previous generated word
            if t == 0:
                x = word_embedding_forward(tf.fill([tf.shape(features)[0]], self._start), self.W_embed)    # (N, M)
            else:
                x = word_embedding_forward(sampled_word, self.W_embed)    # (N, M)

            # generate context vector 
            context, alpha = attention_forward(features, features_projected, prev_h, self.W_proj_h, self.b_proj, self.W_att)
            alpha_list.append(alpha)

            if self.selector:
                beta = affine_sigmoid_forward(prev_h, self.W_sel, self.b_sel)
                context = beta * context

            # rnn/lstm forward prop
            if self.cell_type == 'rnn':
                h = rnn_step_forward(x, prev_h, context, self.Wx, self.Wh, self.Wz, self.b)
                prev_h = h
            elif self.cell_type == 'lstm': 
                h, c = lstm_step_forward(x, prev_h, prev_c, context, self.Wx, self.Wh, self.Wz, self.b) 
                prev_h = h
                prev_c = c

            # generate scores(logits) from current hidden state
            logits = affine_forward(h, self.W1_decode, self.b1_decode)
            if self.prev2out:
                logits += x
            if self.ctx2out:
                logits += tf.matmul(context, self.W_ctx2out)
            logits_h = tf.nn.tanh(logits)
            logits_out = affine_forward(logits_h, self.W2_decode, self.b2_decode)      

            # sample word indices with logits
            sampled_word = tf.argmax(logits_out, 1)        # (N, ) where each element is word index in the range [0, V) 
            sampled_word_list.append(sampled_word)        # tensorflow doesn't provide item assignment 

        alphas = tf.transpose(tf.pack(alpha_list), (1, 0, 2))     # (N, T, L)
        sampled_captions = tf.transpose(tf.pack(sampled_word_list), (1, 0))     # (N, max_len)

        return alphas, sampled_captions