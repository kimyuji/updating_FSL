import os
import copy
import json
import math
import pandas as pd
import torch.nn as nn
import itertools
from backbone import get_backbone_class
import backbone
from datasets.dataloader import get_episodic_dataloader, get_labeled_episodic_dataloader
from datasets.transforms import rand_bbox
from io_utils import parse_args
from model import get_model_class
from model.classifier_head import get_classifier_head_class
from paths import get_output_directory, get_ft_output_directory, get_ft_train_history_path, get_ft_test_history_path,\
    get_final_pretrain_state_path, get_pretrain_state_path, get_ft_params_path, get_ft_v_score_history_path, get_ft_loss_history_path
from utils import *
import time 
from sklearn.cluster import KMeans 
from sklearn.metrics.cluster import v_measure_score
from collections import OrderedDict
import timm
from itertools import chain


def main(params):
    os.environ["CUDA_VISIBLE_DEVICES"] = params.gpu_idx
    device = torch.device(f'cuda:{params.gpu_idx}' if torch.cuda.is_available() else 'cpu')
    print(f"\nCurrently Using GPU {device}\n")
    
    base_output_dir = get_output_directory(params)
    output_dir = get_ft_output_directory(params)
    torch_pretrained = ("torch" in params.backbone or 'vit' in params.backbone)
    if params.pretrained is not None:
        output_dir = output_dir.replace('logs', 'startup')

    print('Running fine-tune with output folder:')
    print(output_dir)
    
    # Settings
    n_episodes = 600
    bs = params.ft_batch_size
    n_data = params.n_way * params.n_shot

    n_epoch = params.ft_epochs
    w = params.n_way
    s = params.n_shot
    q = params.n_query_shot

    # Model
    if 'vit' in params.backbone:
        backbone = get_backbone_class('vit')(params.backbone)
        feature_dim = 768        
    else:
        backbone = get_backbone_class(params.backbone)()
        feature_dim = 512
        
    body = get_model_class(params.model)(backbone, params)

    if params.ft_features is None:
        pass
    else:
        if params.ft_features not in body.supported_feature_selectors:
            raise ValueError(
                'Feature selector "{}" is not supported for model "{}"'.format(params.ft_features, params.model))

    # Dataloaders
    # Note that both dataloaders sample identical episodes, via episode_seed
    support_epochs = n_epoch
    support_loader = get_labeled_episodic_dataloader(params.target_dataset, n_way=w, n_shot=s, support=True,
                                                     n_query_shot=q, n_episodes=n_episodes, n_epochs=support_epochs,
                                                     augmentation=params.ft_augmentation,
                                                     unlabeled_ratio=0,
                                                     num_workers=params.num_workers,
                                                     split_seed=params.split_seed,
                                                     episode_seed=params.ft_episode_seed)

    query_loader = get_labeled_episodic_dataloader(params.target_dataset, n_way=w, n_shot=s, support=False,
                                                   n_query_shot=q, n_episodes=n_episodes, n_epochs=1,
                                                   augmentation=None,
                                                   unlabeled_ratio=0,
                                                   num_workers=params.num_workers,
                                                   split_seed=params.split_seed,
                                                   episode_seed=params.ft_episode_seed)

    assert (len(support_loader) == n_episodes * support_epochs)
    assert (len(query_loader) == n_episodes)

    support_iterator = iter(support_loader)
    support_batches = math.ceil(n_data / bs)
    query_iterator = iter(query_loader)

    # Output (history, params)
    train_history_path = get_ft_train_history_path(output_dir)
    loss_history_path = get_ft_loss_history_path(output_dir)
    test_history_path = get_ft_test_history_path(output_dir)
    support_v_score_history_path, query_v_score_history_path = get_ft_v_score_history_path(output_dir)

    params_path = get_ft_params_path(output_dir)

    print('Saving finetune params to {}'.format(params_path))
    print('Saving finetune train history to {}'.format(train_history_path))
    print('Saving finetune test history to {}'.format(test_history_path))
    print()

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # saving parameters on this json file
    with open(params_path, 'w') as f_batch:
        json.dump(vars(params), f_batch, indent=4)
    
    df_train = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
                            columns=['epoch{}'.format(e + 1) for e in range(n_epoch)])
    df_test = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
                           columns=['epoch{}'.format(e + 1) for e in range(n_epoch)])
    df_loss = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
                           columns=['epoch{}'.format(e + 1) for e in range(n_epoch)])
    # df_v_score_support = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
    #                         columns=['epoch{}'.format(e+1) for e in range(n_epoch)])
    df_v_score_query = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
                           columns=['epoch{}'.format(e) for e in range(n_epoch+1)])
    
    # Pre-train state
    if not torch_pretrained:
        if params.ft_pretrain_epoch is None: # best state
            if params.pretrained is not None:
                if 'dynamic' in params.pretrained:
                    body_state_path = f'./ssl_pretrained/ce_distill_ema_sgd_miniImageNet_{params.target_dataset}_resnet10/best.ckpt'
                elif 'startup' in params.pretrained:
                    body_state_path = f'./ssl_pretrained/student_STARTUP/miniImageNet_source/{params.target_dataset}_unlabeled_20/checkpoint_best.pkl'
            else:
                body_state_path = get_final_pretrain_state_path(base_output_dir)
        
        if params.source_dataset == 'tieredImageNet':
            body_state_path = './logs/baseline/output/pretrained_model/tiered/resnet18_base_LS_base/pretrain_state_0090.pt'

        if not os.path.exists(body_state_path):
            raise ValueError('Invalid pre-train state path: ' + body_state_path)

        print('Using pre-train state:', body_state_path)
        print()
        state = torch.load(body_state_path)

        if params.pretrained is not None and 'startup' in params.pretrained:
            state_body = state['model']
            state_head = state['clf']
            state_simclr = state['clf_SIMCLR']

            new_state = OrderedDict()
            for k, v in state_body.items():
                name = "backbone."+k 
                new_state[name] = v
            for k, v in state_head.items():
                name = "classifier."+k
                new_state[name] = v
            for k, v in state_simclr.items():
                name = "head."+k 
                new_state[name] = v
            state = new_state
    else:
        pass

    mix_bool = (params.ft_mixup or params.ft_cutmix)
    # for cutmix or mixup (between class option)
    if mix_bool:
        all_cases = list(itertools.permutations(list(range(w))))
        class_shuffled = all_cases
        for case in copy.deepcopy(all_cases):
            for idx in range(w):
                if case[idx] == idx:
                    class_shuffled.remove(case)
                    break

    # For each episode
    for episode in range(n_episodes):
        # Reset models for each episode
        if not torch_pretrained:
            body.load_state_dict(copy.deepcopy(state), strict=True)  # note, override model.load_state_dict to change this behavior.
        else:
            body = get_model_class(params.model)(copy.deepcopy(backbone), params)
                           
        head = get_classifier_head_class(params.ft_head)(feature_dim, params.n_way, params)  

        body.cuda()
        head.cuda()
        if params.ft_parts == "head":
            for p in body.parameters():
                p.requires_grad = False
            ft_body_lr = 0.0
            ft_head_lr = params.ft_lr
        else:
            ft_body_lr = params.ft_lr
            ft_head_lr = params.ft_lr

        opt_params = []
        opt_params.append({'params': head.parameters(), 'lr': ft_head_lr, 'momentum' : 0.9, 'dampening' : 0.9, 'weight_decay' : 0.001})
        opt_params.append({'params': body.parameters(), 'lr': ft_body_lr, 'momentum' : 0.9, 'dampening' : 0.9, 'weight_decay' : 0.001})
        
        optimizer = torch.optim.SGD(opt_params)
        criterion = nn.CrossEntropyLoss().cuda()

        x_support = None
        f_support = None
        y_support = torch.arange(w).repeat_interleave(s).cuda()
        y_support_np = y_support.cpu().numpy()

        x_query = next(query_iterator)[0].cuda()
        y_query = torch.arange(w).repeat_interleave(q).cuda() 
        f_query = None
        y_query_np = y_query.cpu().numpy()
        
        train_acc_history = []
        train_loss_history = []
        test_acc_history = []
        support_v_score = []
        query_v_score = []

        # V-measure support and query for epoch 0
        if s != 1:
            support_v_score.append(0.0)

        with torch.no_grad():
            f_query = body_forward(x_query, body, backbone, torch_pretrained, params)
            f_query_np = f_query.cpu().numpy()
            kmeans = KMeans(n_clusters = w)
            cluster_pred = kmeans.fit(f_query_np).labels_
            query_v_score.append(v_measure_score(cluster_pred, y_query_np))

        # For each epoch
        for epoch in range(n_epoch):
            if params.ft_parts == "head" or params.ft_parts == "bn_full":
                body.eval()
            else:
                body.train()
            head.train()

            aug_bool = mix_bool or params.ft_augmentation
            if params.ft_scheduler_end is not None: # if augmentation is scheduled
                aug_bool = (epoch < params.ft_scheduler_end and epoch >= params.ft_scheduler_start) and aug_bool

            x_support = next(support_iterator)[0].cuda()

            total_loss = 0
            correct = 0
            indices = np.random.permutation(w * s) 

            if aug_bool:
                x_support_aug = copy.deepcopy(x_support)
                if mix_bool:
                    if params.ft_mixup:
                        mode = params.ft_mixup
                    elif params.ft_cutmix:
                        mode = params.ft_cutmix
                    else:
                        raise ValueError ("Unknown mode: %s" % mode)
                    
                    lam = np.random.beta(1.0, 1.0)

                    if mode == 'both':
                        indices_shuffled = torch.randperm(x_support.shape[0])
                    else:
                        shuffled = np.array([])
                        if mode == 'within' :
                            class_arr = range(w)
                        elif mode == 'between': 
                            class_arr_idx = np.random.choice(range(len(class_shuffled)), 1)[0]
                            class_arr = class_shuffled[class_arr_idx]

                        for clss in class_arr:
                            shuffled = np.append(shuffled, np.random.permutation(range(clss*s, (clss+1)*s))) 
                        indices_shuffled = torch.from_numpy(shuffled).long()   

                    # mixup
                    if params.ft_mixup:
                        x_support_aug = lam * x_support[:,:,:] + (1. - lam) * x_support[indices_shuffled,:,:]
                        
                    # cutmix
                    elif params.ft_cutmix:
                        bbx1, bby1, bbx2, bby2 = rand_bbox(x_support.shape, lam) 
                        x_support_aug[:,:,bbx1:bbx2, bby1:bby2] = x_support[indices_shuffled,:,bbx1:bbx2, bby1:bby2]
                        lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (x_support.shape[-1] * x_support.shape[-2])) # adjust lambda
                    
                    y_shuffled = y_support[indices_shuffled]

            # For each iteration
            for i in range(support_batches):
                start_index = i * bs
                end_index = min(i * bs + bs, w * s)
                batch_indices = indices[start_index:end_index]

                y_batch = y_support[batch_indices] 

                if aug_bool and mix_bool: 
                    y_shuffled_batch = y_shuffled[batch_indices]


                if aug_bool:
                    f_batch = body_forward(x_support_aug[batch_indices], body, backbone, torch_pretrained, params)
                else:
                    f_batch = body_forward(x_support[batch_indices], body, backbone, torch_pretrained, params)

                pred = head(f_batch)

                correct += torch.eq(y_batch, pred.argmax(dim=1)).sum()

                if aug_bool and mix_bool:
                    loss = criterion(pred, y_batch) * lam + criterion(pred, y_shuffled_batch) * (1. - lam)
                else:
                    loss = criterion(pred, y_batch)

                optimizer.zero_grad() 
                loss.backward() 
                if 'vit' in params.backbone:
                    if params.ft_parts == 'head':
                        torch.nn.utils.clip_grad_norm_(head.parameters(), max_norm=1.)
                    else : 
                        torch.nn.utils.clip_grad_norm_(chain(body.parameters(), head.parameters()), max_norm=1.)
                optimizer.step()

                total_loss += loss.item()

            train_loss = total_loss / support_batches
            train_acc = correct / n_data

            if (epoch+1)%50 == 0 or epoch == n_epoch - 1:
                body.eval()
                head.eval()

                # V-measure support
                if params.v_score and params.n_shot != 1:
                    with torch.no_grad():
                        f_support = body_forward(x_support, body, backbone, torch_pretrained, params)
                    f_support = f_support.cpu().numpy()
                    kmeans = KMeans(n_clusters = w)
                    cluster_pred = kmeans.fit(f_support).labels_
                    support_v_score.append(v_measure_score(cluster_pred, y_support_np))
             
                with torch.no_grad():      
                    # Query Evaluation                 
                    f_query = body_forward(x_query, body, backbone, torch_pretrained, params)
                    pred = head(f_query)
                    correct = torch.eq(y_query, pred.argmax(dim=1)).sum()
                test_acc = correct / pred.shape[0]

                # Query V-measure
                if params.v_score:
                    f_query = f_query.cpu().numpy()
                    kmeans = KMeans(n_clusters = w)
                    cluster_pred = kmeans.fit(f_query).labels_
                    query_v_score.append(v_measure_score(cluster_pred, y_query_np))
            else:
                test_acc = torch.tensor(0)
                support_v_score.append(0.0)
                query_v_score.append(0.0)

            train_acc_history.append(train_acc.item())
            test_acc_history.append(test_acc.item())
            train_loss_history.append(train_loss)

        df_train.loc[episode + 1] = train_acc_history
        df_train.to_csv(train_history_path)
        df_test.loc[episode + 1] = test_acc_history
        df_test.to_csv(test_history_path)
        df_loss.loc[episode + 1] = train_loss_history
        df_loss.to_csv(loss_history_path)

        if params.v_score:
            # if params.n_shot != 1:
                # df_v_score_support.loc[episode + 1] = support_v_score
                # df_v_score_support.to_csv(support_v_score_history_path)
            df_v_score_query.loc[episode + 1] = query_v_score
            df_v_score_query.to_csv(query_v_score_history_path)

        fmt = 'Episode {:03d}: train_loss={:6.4f} train_acc={:6.2f} test_acc={:6.2f}'
        print(fmt.format(episode, train_loss, train_acc_history[-1] * 100, test_acc_history[-1] * 100))

    fmt = 'Final Results: Acc={:5.2f} Std={:5.2f}'
    print(fmt.format(df_test.mean()[-1] * 100, 1.96 * df_test.std()[-1] / np.sqrt(n_episodes) * 100))
    end = time.time()

    print('Saved history to:')
    print(train_history_path)
    print(test_history_path)
    df_train.to_csv(train_history_path)
    df_test.to_csv(test_history_path)
    df_loss.to_csv(loss_history_path)

if __name__ == '__main__':
    np.random.seed(10)
    params = parse_args('train')

    targets = params.target_dataset
    if targets is None:
        targets = [targets]
    elif len(targets) > 1:
        print('#' * 80)
        print("Running finetune iteratively for multiple target datasets: {}".format(targets))
        print('#' * 80)

    for target in targets:
        params.target_dataset = target
        main(params)
