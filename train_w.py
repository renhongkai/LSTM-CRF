from __future__ import print_function
import datetime
import time
import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.optim as optim
import codecs
from model.crf import *
from model.lstm_crf import *
import model.utils as utils
from model.evaluator import eval_w
from model.evaluator import eval_batch

import argparse
import json
import os
import sys
from tqdm import tqdm
import itertools
import functools

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Learning with BLSTM-CRF')
    parser.add_argument('--rand_embedding', action='store_true', help='random initialize word embedding')
    # 预先训练的词向量
    parser.add_argument('--emb_file', default='./embedding/glove.6B.100d.txt', help='path to pre-trained embedding')
    # 训练集
    parser.add_argument('--train_file', default='./data/ner2003/eng.train.iobes', help='path to training file')
    # 测试集
    parser.add_argument('--test_file', default='./data/ner2003/eng.testb.iobes', help='path to test file')
    # gpu
    parser.add_argument('--gpu', type=int, default=0, help='gpu id, set to -1 if use cpu mode')
    parser.add_argument('--batch_size', type=int, default=10, help='batch size (10)')
    # 出现了在预训练的词向量中没有的词，就标为unk
    parser.add_argument('--unk', default='unk', help='unknow-token in pre-trained embedding')
    parser.add_argument('--checkpoint', default='./checkpoint/', help='path to checkpoint prefix')
    # 隐藏状态的维度
    parser.add_argument('--hidden', type=int, default=100, help='hidden dimension')
    parser.add_argument('--drop_out', type=float, default=0.5, help='dropout ratio')
    parser.add_argument('--epoch', type=int, default=80, help='maximum epoch number')
    parser.add_argument('--start_epoch', type=int, default=0, help='start epoch idx')
    parser.add_argument('--caseless', action='store_true', help='caseless or not')
    # 预训练的embedding的维度
    parser.add_argument('--embedding_dim', type=int, default=100, help='dimension for word embedding')
    # lstm的层数
    parser.add_argument('--layers', type=int, default=1, help='number of lstm layers')
    # 学习率
    parser.add_argument('--lr', type=float, default=0.01, help='initial learning rate')
    # 学习率的改变率
    parser.add_argument('--lr_decay', type=float, default=0.001, help='decay ratio of learning rate')
    # 微调预训练的向量
    parser.add_argument('--fine_tune', action='store_false', help='fine tune pre-trained embedding dictionary')
    parser.add_argument('--load_check_point', default='', help='path of checkpoint')
    parser.add_argument('--load_opt', action='store_true', help='load optimizer from ')
    parser.add_argument('--update', choices=['sgd', 'adam'], default='sgd', help='optimizer method')
    # 动量
    parser.add_argument('--momentum', type=float, default=0.9, help='momentum for sgd')
    parser.add_argument('--clip_grad', type=float, default=5.0, help='grad clip at')
    parser.add_argument('--small_crf', action='store_false',help='use small crf instead of large crf, refer model.crf module for more details')
    parser.add_argument('--mini_count', type=float, default=1, help='thresholds to replace rare words with <unk>')
    parser.add_argument('--eva_matrix', choices=['a', 'fa'], default='fa', help='use f1 and accuracy or accuracy alone')
    parser.add_argument('--patience', type=int, default=15, help='patience for early stop')
    parser.add_argument('--least_iters', type=int, default=200, help='at least train how many epochs before stop')
    parser.add_argument('--shrink_embedding', action='store_true',
                        help='shrink the embedding dictionary to corpus (open this if pre-trained embedding dictionary is too large, but disable this may yield better results on external corpus)')
    args = parser.parse_args()

    if args.gpu >= 0:
        torch.cuda.set_device(args.gpu)

    print('setting:')
    print(args)

    # load corpus
    print('loading corpus')
    with codecs.open(args.train_file, 'r', 'utf-8') as f:
        lines = f.readlines()

    with codecs.open(args.test_file, 'r', 'utf-8') as f:
        test_lines = f.readlines()

    test_features, test_labels = utils.read_corpus(test_lines)  #测试集

    if args.load_check_point:
        if os.path.isfile(args.load_check_point):
            print("loading checkpoint: '{}'".format(args.load_check_point))
            checkpoint_file = torch.load(args.load_check_point)
            args.start_epoch = checkpoint_file['epoch']
            f_map = checkpoint_file['f_map']
            l_map = checkpoint_file['l_map']
            train_features, train_labels = utils.read_corpus(lines)
        else:
            print("no checkpoint found at: '{}'".format(args.load_check_point))
    else:
        print('constructing coding table')

        # converting format
        train_features, train_labels, f_map, l_map = utils.generate_corpus(lines, if_shrink_feature=True, thresholds=0)
        f_set = {v for v in f_map}   #f_set是只取f_map中的key值，即训练数据中所有的词，不重复
        f_map = utils.shrink_features(f_map, train_features, args.mini_count)   #args.mini_count默认是5，将稀少的字，即出现不超过5次的字用unk标记

        dt_f_set = functools.reduce(lambda x, y: x | y, map(lambda t: set(t), test_features), f_set)

        if not args.rand_embedding:
            print("feature size: '{}'".format(len(f_map)))  #得到的feature是将稀少的字用unk代替了的
            print('loading embedding')
            if args.fine_tune:  # which means does not do fine-tune
                f_map = {'<eof>': 0}
            f_map, embedding_tensor, in_doc_words = utils.load_embedding_wlm(args.emb_file, ' ', f_map, dt_f_set,
                                                                             args.caseless, args.unk,args.embedding_dim,shrink_to_corpus=args.shrink_embedding)
            print("embedding size: '{}'".format(len(f_map)))   #f_map表示预训练的词向量中所有的词


        l_set = functools.reduce(lambda x, y: x | y, map(lambda t: set(t), test_labels))
        for label in l_set:
            if label not in l_map: #l_map是107行的训练集中的所有标签
                l_map[label] = len(l_map)  #将验证集 测试集中的标签也添加到l_map中
    # construct dataset
    dataset = utils.construct_bucket_mean_vb(train_features, train_labels, f_map, l_map, args.caseless)  #f_map是预训练词向量中的词，l_map是训练集 验证集 测试集中出现的所有标签
    test_dataset = utils.construct_bucket_mean_vb(test_features, test_labels, f_map, l_map, args.caseless)


    dataset_loader = [torch.utils.data.DataLoader(tup, args.batch_size, shuffle=True, drop_last=False) for tup in dataset]
    test_dataset_loader = [torch.utils.data.DataLoader(tup, 50, shuffle=False, drop_last=False) for tup in test_dataset]

    # build model
    print('building model')
    ner_model = LSTM_CRF(len(f_map), len(l_map), args.embedding_dim, args.hidden, args.layers, args.drop_out,large_CRF=args.small_crf)

    print("模型：",ner_model)

    if args.load_check_point:
        ner_model.load_state_dict(checkpoint_file['state_dict'])
    else:
        if not args.rand_embedding:
            ner_model.load_pretrained_embedding(embedding_tensor)
        print('random initialization')
        ner_model.rand_init(init_embedding=args.rand_embedding)

    if args.update == 'sgd':
        optimizer = optim.SGD(ner_model.parameters(), lr=args.lr, momentum=args.momentum)
    elif args.update == 'adam':
        optimizer = optim.Adam(ner_model.parameters(), lr=args.lr)

    if args.load_check_point and args.load_opt:
        optimizer.load_state_dict(checkpoint_file['optimizer'])


    crit = CRFLoss_vb(len(l_map), l_map['<start>'], l_map['<pad>'])
    print("crit:",crit)

    if args.gpu >= 0:
        if_cuda = True
        print('device: ' + str(args.gpu))
        torch.cuda.set_device(args.gpu)   #设置使用gpu
        crit.cuda()
        ner_model.cuda()
        packer = CRFRepack(len(l_map), True)
        print("packer",packer)
    else:
        if_cuda = False
        packer = CRFRepack(len(l_map), False)

    if args.load_check_point:
        test_f1, test_acc = eval_batch(ner_model, test_dataset_loader, packer, l_map)
        print('(checkpoint: F1 on test = %.4f, acc on test= %.4f)' % (test_f1, test_acc))

    tot_length = sum(map(lambda t: len(t), dataset_loader))
    best_f1 = float('-inf')
    best_acc = float('-inf')
    track_list = list()
    start_time = time.time()
    epoch_list = range(args.start_epoch, args.start_epoch + args.epoch)  #range(0, 80)
    patience_count = 0

    evaluator = eval_w(packer, l_map, args.eva_matrix)
    for epoch_idx, args.start_epoch in enumerate(epoch_list):

        epoch_loss = 0
        ner_model.train()

        for feature, tg, mask in tqdm(itertools.chain.from_iterable(dataset_loader), mininterval=2,
                                      desc=' - Tot it %d (epoch %d)' % (tot_length, args.start_epoch), leave=False, file=sys.stdout):
            fea_v, tg_v, mask_v = packer.repack_vb(feature, tg, mask)
            ner_model.zero_grad()
            scores, hidden = ner_model.forward(fea_v)
            loss = crit.forward(scores, tg_v, mask_v)
            loss.backward()
            nn.utils.clip_grad_norm(ner_model.parameters(), args.clip_grad)
            optimizer.step()
            epoch_loss += utils.to_scalar(loss)

        # update lr
        utils.adjust_learning_rate(optimizer, args.lr / (1 + (args.start_epoch + 1) * args.lr_decay))

        # average
        epoch_loss /= tot_length

        if 'f' in args.eva_matrix:
            test_f1, test_pre, test_rec, test_acc = evaluator.calc_score(ner_model, test_dataset_loader)
            track_list.append({'loss': epoch_loss, 'test_f1': test_f1,'test_acc': test_acc})
            print(
                '(loss: %.4f, epoch: %d, F1 on test = %.4f, acc on test= %.4f), saving...' %
                (epoch_loss,args.start_epoch,test_f1,test_acc))
            try:
                utils.save_checkpoint(
                    {'epoch': args.start_epoch,'state_dict': ner_model.state_dict(),'optimizer': optimizer.state_dict(),'f_map': f_map,'l_map': l_map,},
                    {'track_list': track_list,'args': vars(args)},
                        args.checkpoint + 'lstm_crf')
            except Exception as inst:
                print(inst)
        else:
            test_acc = evaluator.calc_score(ner_model, test_dataset_loader)
            track_list.append({'loss': epoch_loss, 'test_acc': test_acc})
            print(
                '(loss: %.4f, epoch: %d, acc on test= %.4f), saving...' %
                (epoch_loss,args.start_epoch,test_acc))
            try:
                utils.save_checkpoint({
                    'epoch': args.start_epoch,
                    'state_dict': ner_model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'f_map': f_map,
                    'l_map': l_map,
                }, {'track_list': track_list,
                    'args': vars(args)
                    }, args.checkpoint + 'lstm_crf')
            except Exception as inst:
                print(inst)

        print('epoch: ' + str(args.start_epoch) + '\t in ' + str(args.epoch) + ' take: ' + str(
            time.time() - start_time) + ' s')

        if patience_count >= args.patience and args.start_epoch >= args.least_iters:
            break

    if 'f' in args.eva_matrix:
        print(
            args.checkpoint + ' test_f1: %.4f test_rec: %.4f test_pre: %.4f test_acc: %.4f\n' % (test_f1, test_rec, test_pre, test_acc))
    else:
        print(args.checkpoint + ' dev_acc: %.4f test_acc: %.4f\n' % (test_acc))

    # printing summary
    print('setting:')
    print(args)
