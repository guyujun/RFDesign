import torch
import torch.nn as nn
from Embeddings import MSA_emb, Pair_emb_wo_templ, Pair_emb_w_templ, Templ_emb
from Attention_module_w_str import IterativeFeatureExtractor
from DistancePredictor import DistanceNetwork
from Refine_module import Refine_module
import Transformer

class TrunkModule(nn.Module):
    def __init__(self, n_module=4, n_module_str=4, n_module_ref=4, n_layer=4,\
                 d_msa=64, d_pair=128, d_templ=64,\
                 n_head_msa=4, n_head_pair=8, n_head_templ=4,
                 d_hidden=64, r_ff=4, n_resblock=1, p_drop=0.0, 
                 performer_L_opts=None, performer_N_opts=None,
                 SE3_param={'l0_in_features':32, 'l0_out_features':16, 'num_edge_features':32}, 
                 REF_param={'l0_in_features':32, 'l0_out_features':16, 'num_edge_features':32}, 
                 use_templ=False, device0=0, device1=0):
        super(TrunkModule, self).__init__()
        self.use_templ = use_templ
        self.device0 = device0
        self.device1 = device1
        #
        self.msa_emb = MSA_emb(d_model=d_msa, p_drop=p_drop, max_len=5000).to(device0)
        if use_templ:
            self.templ_emb = Templ_emb(d_templ=d_templ, n_att_head=n_head_templ, r_ff=r_ff, 
                                       performer_opts=performer_L_opts, p_drop=0.0).to(device0)
            self.pair_emb = Pair_emb_w_templ(d_model=d_pair, d_templ=d_templ, p_drop=p_drop).to(device0)
        else:
            self.pair_emb = Pair_emb_wo_templ(d_model=d_pair, p_drop=p_drop).to(device0)
        #
        self.feat_extractor = IterativeFeatureExtractor(n_module=n_module,\
                                                        n_module_str=n_module_str,\
                                                        n_layer=n_layer,\
                                                        d_msa=d_msa, d_pair=d_pair, d_hidden=d_hidden,\
                                                        n_head_msa=n_head_msa, \
                                                        n_head_pair=n_head_pair,\
                                                        r_ff=r_ff, \
                                                        n_resblock=n_resblock,
                                                        p_drop=p_drop,
                                                        performer_N_opts=performer_N_opts,
                                                        performer_L_opts=performer_L_opts,
                                                        SE3_param=SE3_param,
                                                        device0=device0,
                                                        device1=device1)
        self.c6d_predictor = DistanceNetwork(d_pair, p_drop=p_drop).to(device1)
        #
        self.refine = Refine_module(n_module_ref, d_node=d_msa, d_pair=130,
                                    d_node_hidden=d_hidden, d_pair_hidden=d_hidden,
                                    SE3_param=REF_param, p_drop=p_drop).to(device1)

    def forward(self, msa, seq=None, idx=None, t1d=None, t2d=None, msa_one_hot=None,
                prob_s=None, trunk_only=False, refine_only=False, use_transf_checkpoint=False):

        out = {}

        if not refine_only:
            B, N, L = msa.shape

            if seq is None:
                seq = msa[:,0]

            if idx is None:
                idx = torch.arange(L, device=msa.device).unsqueeze(0).expand(B,-1)

            # Get embeddings
            if msa_one_hot is not None:
                msa = self.msa_emb(msa, idx, msa_one_hot)
                if self.use_templ:
                    if t1d is None:
                        t1d = torch.zeros((B, 1, L, 3), device=msa.device).float()
                        t2d = torch.zeros((B, 1, L, L, 10), device=msa.device).float()
                    tmpl = self.templ_emb(t1d, t2d, idx)
                    pair = self.pair_emb(seq, idx, tmpl, msa_one_hot[:,0])
                else:
                    pair = self.pair_emb(seq, idx, msa_one_hot[:,0])
            else:
                msa = self.msa_emb(msa, idx)
                if self.use_templ:
                    if t1d is None:
                        t1d = torch.zeros((B, 1, L, 3), device=msa.device).float()
                        t2d = torch.zeros((B, 1, L, L, 10), device=msa.device).float()
                    tmpl = self.templ_emb(t1d, t2d, idx)
                    pair = self.pair_emb(seq, idx, tmpl)
                else:
                    pair = self.pair_emb(seq, idx)

            # Extract features
            if msa_one_hot is not None:
                seq1hot = msa_one_hot[:,0]
            else:
                seq1hot = torch.nn.functional.one_hot(seq, num_classes=21).float()

            msa, pair, xyz, lddt = self.feat_extractor(msa, pair, seq1hot, idx, 
                                                       use_transf_checkpoint=use_transf_checkpoint)

            # Predict 6D coords
            logits = self.c6d_predictor(pair)
            
            prob_s = list()
            for l in logits:
                prob_s.append(nn.Softmax(dim=1)(l)) # (B, C, L, L)
            prob_s = torch.cat(prob_s, dim=1).permute(0,2,3,1)
        
            out['dist'] = logits[0]
            out['omega'] = logits[1]
            out['theta'] = logits[2]
            out['phi'] = logits[3]
            out['xyz'] = xyz.view(B,L,3,3)
            out['lddt'] = lddt.view(B, L)
            out['prob_s'] = prob_s

        if trunk_only:
            return out
            #return logits, msa, xyz, lddt.view(B, L)
        
        B, L = msa.shape[:2]
        ref_xyz, ref_lddt = self.refine(msa, prob_s, seq1hot.to(self.device1), idx.to(self.device1), 
                                        use_transf_checkpoint=use_transf_checkpoint)

        out['xyz'] = ref_xyz.view(B,L,3,3)
        out['lddt'] = ref_lddt.view(B,L)
        return out

        #if refine_only:
        #    return ref_xyz, ref_lddt.view(B,L)
        #else:
        #    return logits, msa, ref_xyz, ref_lddt.view(B,L)