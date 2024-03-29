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

def main(params):
    os.environ["CUDA_VISIBLE_DEVICES"] = params.gpu_idx
    device = torch.device(f'cuda:{params.gpu_idx}' if torch.cuda.is_available() else 'cpu')
    print(f"\nCurrently Using GPU {device}\n")
    
    base_output_dir = get_output_directory(params) 
    output_dir = get_ft_output_directory(params)
    torch_pretrained = ("torch" in params.backbone)

    print('Running fine-tune with output folder:')
    print(output_dir)
    
    lr_list = [0.01, 0.005, 0.001, 0.0005, 0.0001]
    n_epoch = 100
    for lr in lr_list:
        # Settings
        n_episodes = 600
        bs = params.ft_batch_size
        n_data = params.n_way * params.n_shot

        w = params.n_way
        s = params.n_shot
        q = params.n_query_shot

        # Model
        backbone = get_backbone_class(params.backbone)()
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
        train_history_path = get_ft_train_history_path(output_dir).replace('.csv', '_{}_{}.csv'.format(n_epoch, lr))
        loss_history_path = get_ft_loss_history_path(output_dir).replace('.csv', '_{}_{}.csv'.format(n_epoch, lr))
        test_history_path = get_ft_test_history_path(output_dir).replace('.csv', '_{}_{}.csv'.format(n_epoch, lr))

        params_path = get_ft_params_path(output_dir)

        print('Saving finetune params to {}'.format(params_path))
        print('Saving finetune train history to {}'.format(train_history_path))
        print('Saving finetune test history to {}'.format(test_history_path))
        print()

        # saving parameters on this json file
        with open(params_path, 'w') as f_batch:
            json.dump(vars(params), f_batch, indent=4)

        df_train = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
                                columns=['epoch{}'.format(e + 1) for e in range(n_epoch)])
        df_test = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
                               columns=['epoch{}'.format(e + 1) for e in range(n_epoch)])
        df_loss = pd.DataFrame(None, index=list(range(1, n_episodes + 1)),
                               columns=['epoch{}'.format(e + 1) for e in range(n_epoch)])

        # Pre-train state
        if not torch_pretrained:
            if params.ft_pretrain_epoch is None: # best state
                body_state_path = get_final_pretrain_state_path(base_output_dir)

            if params.source_dataset == 'tieredImageNet':
                body_state_path = './logs/baseline/output/pretrained_model/tiered/resnet18_base_LS_base/pretrain_state_0090.pt'

            if not os.path.exists(body_state_path):
                raise ValueError('Invalid pre-train state path: ' + body_state_path)

            print('Using pre-train state:', body_state_path)
            print()
            state = torch.load(body_state_path)
        else:
            pass


        # For each episode
        for episode in range(n_episodes):
            # Reset models for each episode
            if not torch_pretrained:
                body.load_state_dict(copy.deepcopy(state))  # note, override model.load_state_dict to change this behavior.
            else:
                body = get_model_class(params.model)(copy.deepcopy(backbone), params)

            head = get_classifier_head_class(params.ft_head)(512, params.n_way, params)  

            body.cuda()
            head.cuda()
            if params.ft_parts == "head":
                for p in body.parameters():
                    p.requires_grads = False
                params.ft_body_lr = 0.0
            else:
                pass

            opt_params = []
            opt_params.append({'params': head.parameters(), 'lr': lr, 'momentum' : 0.9, 'dampening' : 0.9, 'weight_decay' : 0.001})
            opt_params.append({'params': body.parameters(), 'lr': lr, 'momentum' : 0.9, 'dampening' : 0.9, 'weight_decay' : 0.001})

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
                if params.ft_parts == "head":
                    body.eval()
                else:
                    body.train()
                head.train()

                x_support = next(support_iterator)[0].cuda()

                total_loss = 0
                correct = 0
                indices = np.random.permutation(w * s) 

                # For each iteration
                for i in range(support_batches):
                    start_index = i * bs
                    end_index = min(i * bs + bs, w * s)
                    batch_indices = indices[start_index:end_index]

                    y_batch = y_support[batch_indices] 
                    f_batch = body_forward(x_support[batch_indices], body, backbone, torch_pretrained, params)

                    pred = head(f_batch)
                    correct += torch.eq(y_batch, pred.argmax(dim=1)).sum()
                    loss = criterion(pred, y_batch)

                    optimizer.zero_grad() 
                    loss.backward() 
                    optimizer.step()

                    total_loss += loss.item()

                train_loss = total_loss / support_batches
                train_acc = correct / n_data

                if params.ft_intermediate_test or epoch == n_epoch - 1:
                    body.eval()
                    head.eval()

                    with torch.no_grad():      
                        # Query Evaluation                 
                        f_query = body_forward(x_query, body, backbone, torch_pretrained, params)
                        pred = head(f_query)
                        correct = torch.eq(y_query, pred.argmax(dim=1)).sum()
                    test_acc = correct / pred.shape[0]

                else:
                    test_acc = torch.tensor(0)

                train_acc_history.append(train_acc.item())
                test_acc_history.append(test_acc.item())
                train_loss_history.append(train_loss)

            df_train.loc[episode + 1] = train_acc_history
            df_train.to_csv(train_history_path)
            df_test.loc[episode + 1] = test_acc_history
            df_test.to_csv(test_history_path)
            df_loss.loc[episode + 1] = train_loss_history
            df_loss.to_csv(loss_history_path)

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
