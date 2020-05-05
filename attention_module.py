"""Implementation of the deep relational architecture used in https://arxiv.org/pdf/1806.01830.pdf.
"""
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math
import torch.optim
import torch.autograd
from collections import OrderedDict

class AttentionHead(nn.Module):

    def __init__(self, n_elems, elem_size, emb_size):
        super(AttentionHead, self).__init__()
        self.sqrt_emb_size = int(math.sqrt(emb_size))
        #queries, keys, values
        self.query = nn.Linear(elem_size, emb_size)
        self.key = nn.Linear(elem_size, emb_size)
        self.value = nn.Linear(elem_size, elem_size)
        #layer norms:
        # From 2018 version of paper it sounds like individual projected
        # entities q_i, k_i, v_i are 0,1 normalized (layer norm). However it's unclear whether and how gain and bias
        # are learned, e.g. one gain and bias for each projected entity, or for each element in the vectors of each
        # entity (and whether these are shared), or not at all?
        #
        #https://github.com/gyh75520/Relational_DRL/blob/a5f0d478e16b961d2bd1640952f1328046f85672/ opted to have
        # separate gains and biases for every embedding entry in q,k,v separately and not shared across heads

        # utils.py  # L100
        # For now I decided to not use an affine transform in the end.
        self.qln = nn.LayerNorm([n_elems, emb_size], elementwise_affine=True)
        self.kln = nn.LayerNorm([n_elems, emb_size], elementwise_affine=True)
        self.vln = nn.LayerNorm([n_elems, elem_size], elementwise_affine=True)

    def forward(self, x):
        # print(f"input: {x.shape}")
        Q = self.qln(self.query(x))
        K = self.kln(self.key(x))
        V = self.vln(self.value(x))
        # print(f"Q:{Q.shape}, K:{K.shape}, V:{V.shape}")
        # print(f"QK': {torch.bmm(Q,K.transpose(1,2)).shape}")
        softmax = F.softmax(torch.bmm(Q,K.transpose(1,2))/self.sqrt_emb_size, dim=-1)
        # print(f"softmax: {torch.sum(softmax[0,0,:])}")
        output = torch.bmm(softmax,V)
        # print(f"output: {output.shape}")
        return output

class AttentionModule(nn.Module):

    def __init__(self, n_elems, elem_size, emb_size, n_heads):
        super(AttentionModule, self).__init__()
        # self.input_shape = input_shape
        # self.elem_size = elem_size
        # self.emb_size = emb_size #honestly not really needed
        self.heads =  nn.ModuleList(AttentionHead(n_elems, elem_size, emb_size) for _ in range(n_heads))
        self.linear1 = nn.Linear(n_heads*elem_size, elem_size)
        self.linear2 = nn.Linear(elem_size, elem_size)

        self.ln = nn.LayerNorm(elem_size, elementwise_affine=True)

    def forward(self, x):
        #concatenate all heads' outputs
        A_cat = torch.cat([head(x) for head in self.heads], -1)
        # projecting down to original element size with 2-layer MLP, layer size = entity size
        mlp_out = F.relu(self.linear2(F.relu(self.linear1(A_cat))))
        # residual connection and final layer normalization
        return self.ln(x + mlp_out)

class DRRLnet(nn.Module):

    def __init__(self, h, w, outputs, n_f_conv1 = 12, n_f_conv2 = 24,
                 att_emb_size=64, n_heads=2, n_att_stack=2, n_fc_layers=4, pad=True,
                 baseline_mode=False, n_baseMods=3):
        """
        Args:
            baseline: True means that instead of the attentional module, a n_baseline number of residual-convolutional
                blocks will be placed at the core of the model instead of the attentional module.
        """

        #internal action replay buffer for simple training algorithms
        self.baseline_mode = baseline_mode
        self.saved_actions = []
        self.rewards = []

        self.pad = pad
        self.n_baseMods = n_baseMods
        super(DRRLnet, self).__init__()

        self.conv1 = nn.Conv2d(3, n_f_conv1, kernel_size=2, stride=1)
        #possibly batch or layer norm, neither was mentioned in the paper though
        # self.ln1 = nn.LayerNorm([n_f_conv1,conv1w,conv1h])
        # self.bn1 = nn.BatchNorm2d(n_f_conv1)
        self.conv2 = nn.Conv2d(n_f_conv1, n_f_conv2, kernel_size=2, stride=1)
        # self.ln2 = nn.LayerNorm([n_f_conv2,conv2w,conv2h])
        # self.bn2 = nn.BatchNorm2d(n_f_conv2)

        # calculate size of convolution module output
        def conv2d_size_out(size, kernel_size=2, stride=1):
            return (size - (kernel_size - 1) - 1) // stride + 1
        if self.pad:
            conv1w = conv2w = w
            conv1h = conv2h = h
        else:
            conv1w = conv2d_size_out(w)
            conv1h = conv2d_size_out(h)
            conv2w = conv2d_size_out(conv1w)
            conv2h = conv2d_size_out(conv1h)

        # create x,y coordinate matrices to append to convolution output
        xmap = np.linspace(-np.ones(conv2h), np.ones(conv2h), num=conv2w, endpoint=True, axis=0)
        xmap = torch.tensor(np.expand_dims(np.expand_dims(xmap,0),0), dtype=torch.float32, requires_grad=False)
        ymap = np.linspace(-np.ones(conv2w), np.ones(conv2w), num=conv2h, endpoint=True, axis=1)
        ymap = torch.tensor(np.expand_dims(np.expand_dims(ymap,0),0), dtype=torch.float32, requires_grad=False)
        self.register_buffer("xymap", torch.cat((xmap,ymap),dim=1)) # shape (1, 2, conv2w, conv2h)

        # an "attendable" entity has 24 CNN channels + 2 coordinate channels = 26 features. this is also the default
        # number of baseline module conv layer filter number
        att_elem_size = n_f_conv2 + 2
        if not self.baseline_mode:
            # create attention module with n_heads heads and remember how many times to stack it
            self.n_att_stack = n_att_stack #how many times the attentional module is to be stacked (weight-sharing -> reuse)
            self.attMod = AttentionModule(conv2w*conv2h, att_elem_size, att_emb_size, n_heads)
        else:            # create baseline module of several residual-convolutional layers
            base_dict = {}
            for i in range(self.n_baseMods):
                base_dict[f"baseline_identity_{i}"] = nn.Identity()
                base_dict[f"baseline_conv_{i}_0"] = nn.Conv2d(att_elem_size, att_elem_size, kernel_size=3, stride=1)
                base_dict[f"baseline_batchnorm_{i}_0"] = nn.BatchNorm2d(att_elem_size)
                base_dict[f"baseline_conv_{i}_1"] = nn.Conv2d(att_elem_size, att_elem_size, kernel_size=3, stride=1)
                base_dict[f"baseline_batchnorm_{i}_1"] = nn.BatchNorm2d(att_elem_size)

            self.baseMod = nn.ModuleDict(base_dict)
        #max pooling
        # print(f"attnl element size:{att_elem_size}")
        # self.maxpool = nn.MaxPool1d(kernel_size=att_emb_size,return_indices=False) #don't know why maxpool reduces
        # kernel_size by 1

        # FC256 layers, 4 is default
        if n_fc_layers < 1:
            raise ValueError("At least 1 linear readout layer is required.")
        fc_dict = OrderedDict([('fc1', nn.Linear(att_elem_size, 256)),
                               ('relu1', nn.ReLU())]) #first one has different inpuz size
        for i in range(n_fc_layers-1):
            fc_dict[f"fc{i+2}"] = nn.Linear(256, 256)
            fc_dict[f"relu{i+2}"] = nn.ReLU()
        self.fc_seq = nn.Sequential(fc_dict) #sequential container from ordered dict
        self.logits = nn.Linear(256, outputs)
        self.value = nn.Linear(256, 1)

    # Called with either one element to determine next action, or a batch
    # during optimization. Returns tensor([[left0exp,right0exp]...]).
    def forward(self, x):
        #convolutional module
        if self.pad:
            x = F.pad(x, (1,0,1,0)) #zero padding so state size stays constant
        c = F.relu(self.conv1(x))
        if self.pad:
            c = F.pad(c, (1,0,1,0))
        c = F.relu(self.conv2(c))
        #append x,y coordinates to every sample in batch
        batchsize = c.size(0)
        # Filewriter complains about the this way of repeating the xymap, hope repeat is just as fine
        # batch_maps = torch.cat(batchsize*[self.xymap])
        batch_maps = self.xymap.repeat(batchsize,1,1,1,)
        c = torch.cat((c,batch_maps),1)
        if not self.baseline_mode:
            #attentional module
            #careful: we are flattening out x,y dimensions into 1 dimension, so shape changes from (batchsize, #filters,
            # #conv2w, conv2h) to (batchsize, conv2w*conv2h, #filters), because downstream linear layers take last
            # dimension to be input features
            a = c.view(c.size(0),c.size(1), -1).transpose(1,2)
            # n_att_mod passes through attentional module -> n_att_mod stacked modules with weight sharing
            for i_att in range(self.n_att_stack):
                a = self.attMod(a)
        else:
            #baseline module
            for i in range(self.n_baseMods):
                inp = self.baseMod[f"baseline_identity_{i}"](c)         #save input for residual connection
                #todo: make padding adaptive to kernel size and stride
                c = F.pad(c, (1, 1, 1, 1))                              #padding so input maintains size
                c = self.baseMod[f"baseline_conv_{i}_0"](c)             #conv1
                c = self.baseMod[f"baseline_batchnorm_{i}_0"](c)        #batch-norm
                c = F.relu(c)                                           #relu
                c = F.pad(c, (1, 1, 1, 1))                              #padding so input maintains size
                c = self.baseMod[f"baseline_conv_{i}_1"](c)             #conv2
                c = c + inp                                             #residual connecton
                c = self.baseMod[f"baseline_batchnorm_{i}_1"](c)        #batch-norm
                c = F.relu(c)                                           #relu
            a = c.view(c.size(0),c.size(1), -1).transpose(1,2)          #flatten (transpose not necessary but we do
                                                                        # it for consistency w/ attentional module

        #max pooling over "space", i.e. max scalar within each feature map m x n x f -> f
        # pool over entity dimension #isn't this a problem with gradients?
        # todo: try pooling over feature dimension
        kernelsize = a.shape[1] #but during forward passes called by SummaryWriter, a.shape[1] returns a tensor instead
        # of an int. if this causes any trouble it can be replaced by w*h
        if type(kernelsize) == torch.Tensor:
            kernelsize = kernelsize.item()
        pooled = F.max_pool1d(a.transpose(1,2), kernel_size=kernelsize) #pool out entity dimension
        #policy module: 4xFC256, then project to logits and value
        p = self.fc_seq(pooled.view(pooled.size(0),pooled.size(1)))
        pi = F.softmax(self.logits(p), dim=1)
        v = self.value(p) #todo: no normalization?
        return pi, v
