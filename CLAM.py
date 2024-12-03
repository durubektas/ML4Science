import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import pdb

from MIL_layers import get_attn_module

"""
args:
    gated: whether to use gated attention network
    size_arg: config for network size
    dropout:value of dropout
    k_sample: number of positive/neg patches to sample for instance-level training
    n_classes: number of classes 
    instance_loss_fn: loss function to supervise instance-level training
    subtyping: whether it's a subtyping problem
"""

class CLAM_SB(nn.Module):
    def __init__(self, gated = True, size_arg = "small", dropout = 0., k_sample=8, n_classes=2,
        instance_loss_fn=nn.CrossEntropyLoss(), subtyping=False, embed_size=1024,bag_weight=0.5):
        super().__init__()
        self.size_dict = {"small": [embed_size, 512, 256], "big": [embed_size, 512, 384]}
        size = self.size_dict[size_arg]
        fc = [nn.Linear(size[0], size[1]), nn.ReLU(), nn.Dropout(dropout)]
        
        attention_net = get_attn_module(size[1], size[2], 1, dropout, gated)

        fc.append(attention_net)
        self.attention_net = nn.Sequential(*fc)
        self.classifiers = nn.Linear(size[1], n_classes)
        instance_classifiers = [nn.Linear(size[1], 2) for i in range(n_classes)]
        self.instance_classifiers = nn.ModuleList(instance_classifiers)
        self.k_sample = k_sample
        self.instance_loss_fn = instance_loss_fn
        self.n_classes = n_classes
        self.subtyping = subtyping
        self.bag_weight = bag_weight
    
    @staticmethod
    def create_positive_targets(length, device):
        return torch.full((length, ), 1, device=device).long()
    
    @staticmethod
    def create_negative_targets(length, device):
        return torch.full((length, ), 0, device=device).long()
    
    #instance-level evaluation for in-the-class attention branch
    def inst_eval(self, A, h, classifier): 
        device=h.device
        if len(A.shape) == 1:
            A = A.view(1, -1)
        top_p_ids = torch.topk(A, self.k_sample)[1][-1]
        top_p = torch.index_select(h, dim=0, index=top_p_ids)
        top_n_ids = torch.topk(-A, self.k_sample, dim=1)[1][-1]
        top_n = torch.index_select(h, dim=0, index=top_n_ids)
        p_targets = self.create_positive_targets(self.k_sample, device)
        n_targets = self.create_negative_targets(self.k_sample, device)

        all_targets = torch.cat([p_targets, n_targets], dim=0)
        all_instances = torch.cat([top_p, top_n], dim=0)
        logits = classifier(all_instances)
        all_preds = torch.topk(logits, 1, dim = 1)[1].squeeze(1)
        instance_loss = self.instance_loss_fn(logits, all_targets)
        return instance_loss, all_preds, all_targets
    
    #instance-level evaluation for out-of-the-class attention branch
    def inst_eval_out(self, A, h, classifier):
        device=h.device
        if len(A.shape) == 1:
            A = A.view(1, -1)
        top_p_ids = torch.topk(A, self.k_sample)[1][-1]
        top_p = torch.index_select(h, dim=0, index=top_p_ids)
        p_targets = self.create_negative_targets(self.k_sample, device)
        logits = classifier(top_p)
        p_preds = torch.topk(logits, 1, dim = 1)[1].squeeze(1)
        instance_loss = self.instance_loss_fn(logits, p_targets)
        return instance_loss, p_preds, p_targets

    def forward(self, h, label=None, instance_eval=False, return_features=False, attention_only=False):
        A,h = self.attention_net(h.squeeze(0))  # NxK        
        A = torch.transpose(A, 1, 0)  # KxN
        if attention_only:
            return A
        A_raw = A
        A = F.softmax(A, dim=1)  # softmax over N

        if instance_eval:
            total_inst_loss = 0.0
            all_preds = []
            all_targets = []
            inst_labels = F.one_hot(label, num_classes=self.n_classes).squeeze() #binarize label
            for i in range(len(self.instance_classifiers)):
                inst_label = inst_labels[i].item()
                classifier = self.instance_classifiers[i]
                if inst_label == 1: #in-the-class:
                    instance_loss, preds, targets = self.inst_eval(A, h, classifier)
                    all_preds.extend(preds.cpu().numpy())
                    all_targets.extend(targets.cpu().numpy())
                else: #out-of-the-class
                    if self.subtyping:
                        instance_loss, preds, targets = self.inst_eval_out(A, h, classifier)
                        all_preds.extend(preds.cpu().numpy())
                        all_targets.extend(targets.cpu().numpy())
                    else:
                        continue
                total_inst_loss += instance_loss

            if self.subtyping:
                total_inst_loss /= len(self.instance_classifiers)
                
        M = torch.mm(A, h) 
        logits = self.classifiers(M)
        Y_hat = torch.topk(logits, 1, dim = 1)[1]
        Y_prob = F.softmax(logits, dim = 1)
        if instance_eval:
            results_dict = {'instance_loss': total_inst_loss, 'inst_labels': np.array(all_targets), 
            'inst_preds': np.array(all_preds)}
        else:
            results_dict = {}
        if return_features:
            results_dict.update({'features': M})
        return logits, Y_prob, Y_hat, A_raw, results_dict
    
    def calculate_classification_error(self, X, Y):
            Y = Y.float()
            _,_, Y_hat, _,_ = self.forward(X)
            error = 1. - Y_hat.eq(Y).cpu().float().mean().data.item()

            return error, Y_hat

    def calculate_objective(self, X, Y):
        Y = Y.long()
        Y = Y.unsqueeze(0)  
        logits, Y_prob, Y_hat, _, instance_dict = self.forward(X,instance_eval=True,label=Y)
        _,Y_prob, _, A,_ = self.forward(X)
        Y_prob = torch.clamp(Y_prob, min=1e-5, max=1. - 1e-5)
        loss = self.instance_loss_fn(logits, Y)
        instance_loss = instance_dict['instance_loss']
        total_loss = self.bag_weight * loss + (1-self.bag_weight) * instance_loss 

        return total_loss, A

# class CLAM_MB(CLAM_SB):
#     def __init__(self, gate = True, size_arg = "small", dropout = 0., k_sample=8, n_classes=2,
#         instance_loss_fn=nn.CrossEntropyLoss(), subtyping=False, embed_size=1024):
#         nn.Module.__init__(self)
#         self.size_dict = {"small": [embed_size, 512, 256], "big": [embed_size, 512, 384]}
#         size = self.size_dict[size_arg]
#         fc = [nn.Linear(size[0], size[1]), nn.ReLU(), nn.Dropout(dropout)]
#         if gate:
#             attention_net = Attn_Net_Gated(L = size[1], D = size[2], dropout = dropout, n_classes = n_classes)
#         else:
#             attention_net = Attn_Net(L = size[1], D = size[2], dropout = dropout, n_classes = n_classes)
#         fc.append(attention_net)
#         self.attention_net = nn.Sequential(*fc)
#         bag_classifiers = [nn.Linear(size[1], 1) for i in range(n_classes)] #use an indepdent linear layer to predict each class
#         self.classifiers = nn.ModuleList(bag_classifiers)
#         instance_classifiers = [nn.Linear(size[1], 2) for i in range(n_classes)]
#         self.instance_classifiers = nn.ModuleList(instance_classifiers)
#         self.k_sample = k_sample
#         self.instance_loss_fn = instance_loss_fn
#         self.n_classes = n_classes
#         self.subtyping = subtyping

#     def forward(self, h, label=None, instance_eval=False, return_features=False, attention_only=False):
#         A, h = self.attention_net(h)  # NxK        
#         A = torch.transpose(A, 1, 0)  # KxN
#         if attention_only:
#             return A
#         A_raw = A
#         A = F.softmax(A, dim=1)  # softmax over N

#         if instance_eval:
#             total_inst_loss = 0.0
#             all_preds = []
#             all_targets = []
#             inst_labels = F.one_hot(label, num_classes=self.n_classes).squeeze() #binarize label
#             for i in range(len(self.instance_classifiers)):
#                 inst_label = inst_labels[i].item()
#                 classifier = self.instance_classifiers[i]
#                 if inst_label == 1: #in-the-class:
#                     instance_loss, preds, targets = self.inst_eval(A[i], h, classifier)
#                     all_preds.extend(preds.cpu().numpy())
#                     all_targets.extend(targets.cpu().numpy())
#                 else: #out-of-the-class
#                     if self.subtyping:
#                         instance_loss, preds, targets = self.inst_eval_out(A[i], h, classifier)
#                         all_preds.extend(preds.cpu().numpy())
#                         all_targets.extend(targets.cpu().numpy())
#                     else:
#                         continue
#                 total_inst_loss += instance_loss

#             if self.subtyping:
#                 total_inst_loss /= len(self.instance_classifiers)

#         M = torch.mm(A, h) 

#         logits = torch.empty(1, self.n_classes).float().to(M.device)
#         for c in range(self.n_classes):
#             logits[0, c] = self.classifiers[c](M[c])

#         Y_hat = torch.topk(logits, 1, dim = 1)[1]
#         Y_prob = F.softmax(logits, dim = 1)
#         if instance_eval:
#             results_dict = {'instance_loss': total_inst_loss, 'inst_labels': np.array(all_targets), 
#             'inst_preds': np.array(all_preds)}
#         else:
#             results_dict = {}
#         if return_features:
#             results_dict.update({'features': M})
#         return logits, Y_prob, Y_hat, A_raw, results_dict