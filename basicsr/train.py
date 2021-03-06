import argparse
import logging
import math
import random
import time
import torch
from mmcv.runner import get_time_str, init_dist
from os import path as osp

from basicsr.data import create_dataloader, create_dataset
from basicsr.data.data_sampler import DistIterSampler
from basicsr.data.prefetch_dataloader import CPUPrefetcher, CUDAPrefetcher
from basicsr.models import create_model
from basicsr.utils import (MessageLogger, check_resume, get_env_info,
                           get_root_logger, init_tb_logger, init_wandb_logger,
                           make_exp_dirs, set_random_seed)
from basicsr.utils.options import dict2str, parse


def main():
    # options
    parser = argparse.ArgumentParser()
    parser.add_argument('-opt', type=str, help='Path to option YAML file.')
    parser.add_argument(
        '--launcher',
        choices=['none', 'pytorch', 'slurm'],
        default='none',
        help='job launcher')
    parser.add_argument('--local_rank', type=int, default=0)
    args = parser.parse_args()
    opt = parse(args.opt, is_train=True)

    # distributed training settings
    if args.launcher == 'none':  # disabled distributed training
        opt['dist'] = False
        rank = 0
        print('Disabled distributed training.', flush=True)
    else:
        opt['dist'] = True
        if args.launcher == 'slurm' and 'dist_params' in opt:
            init_dist(args.launcher, **opt['dist_params'])
        else:
            init_dist(args.launcher)
        world_size = torch.distributed.get_world_size()
        rank = torch.distributed.get_rank()
    opt['rank'] = rank

    # load resume states if exists
    if opt['path'].get('resume_state', None):
        device_id = torch.cuda.current_device()
        resume_state = torch.load(
            opt['path']['resume_state'],
            map_location=lambda storage, loc: storage.cuda(device_id))
    else:
        resume_state = None

    # mkdir and loggers
    if resume_state is None:
        make_exp_dirs(opt)
    log_file = osp.join(opt['path']['log'],
                        f"train_{opt['name']}_{get_time_str()}.log")
    logger = get_root_logger(
        logger_name='basicsr', log_level=logging.INFO, log_file=log_file)
    logger.info(get_env_info())
    logger.info(dict2str(opt))
    # initialize tensorboard logger and wandb logger
    tb_logger = None
    if opt['logger'].get('use_tb_logger') and 'debug' not in opt['name']:
        tb_logger = init_tb_logger(log_dir='./tb_logger/' + opt['name'])
    if (opt['logger'].get('wandb')
            is not None) and (opt['logger']['wandb'].get('project')
                              is not None) and ('debug' not in opt['name']):
        assert opt['logger'].get('use_tb_logger') is True, (
            'should turn on tensorboard when using wandb')
        init_wandb_logger(opt)

    # random seed
    seed = opt['train']['manual_seed']
    if seed is None:
        seed = random.randint(1, 10000)
    logger.info(f'Random seed: {seed}')
    set_random_seed(seed + rank)

    torch.backends.cudnn.benchmark = True
    # torch.backends.cudnn.deterministic = True

    # create train and val dataloaders
    train_loader, val_loader = None, None
    for phase, dataset_opt in opt['datasets'].items():
        if phase == 'train':
            # dataset_ratio: enlarge the size of datasets for each epoch
            dataset_enlarge_ratio = dataset_opt.get('dataset_enlarge_ratio', 1)
            train_set = create_dataset(dataset_opt)
            train_size = int(
                math.ceil(len(train_set) / dataset_opt['batch_size']))
            total_iters = int(opt['train']['niter'])
            total_epochs = int(math.ceil(total_iters / train_size))
            if opt['dist']:
                train_sampler = DistIterSampler(train_set, world_size, rank,
                                                dataset_enlarge_ratio)
                total_epochs = total_iters / (
                    train_size * dataset_enlarge_ratio)
                total_epochs = int(math.ceil(total_epochs))
            else:
                train_sampler = None
            train_loader = create_dataloader(train_set, dataset_opt, opt,
                                             train_sampler)
            logger.info(f'Number of train images: {len(train_set)}, '
                        f'iters: {train_size}')
            logger.info(
                f'Total epochs needed: {total_epochs} for iters {total_iters}')
        elif phase == 'val':
            val_set = create_dataset(dataset_opt)
            val_loader = create_dataloader(val_set, dataset_opt, opt, None)
            logger.info(
                f"Number of val images/folders in {dataset_opt['name']}: "
                f'{len(val_set)}')
        else:
            raise NotImplementedError(f'Phase {phase} is not recognized.')
    assert train_loader is not None

    # create model
    if resume_state:
        check_resume(opt, resume_state['iter'])  # modify pretrain_model paths
    model = create_model(opt)

    # resume training
    if resume_state:
        logger.info(f"Resuming training from epoch: {resume_state['epoch']}, "
                    f"iter: {resume_state['iter']}.")
        start_epoch = resume_state['epoch']
        current_iter = resume_state['iter']
        model.resume_training(resume_state)  # handle optimizers and schedulers
    else:
        current_iter = 0
        start_epoch = 0

    # create message logger (formatted outputs)
    msg_logger = MessageLogger(opt, current_iter, tb_logger)

    # training
    logger.info(
        f'Start training from epoch: {start_epoch}, iter: {current_iter}')
    data_time, iter_time = 0, 0

    # dataloader prefetcher
    prefetch_mode = opt['datasets']['train'].get('prefetch_mode')
    if prefetch_mode is None or prefetch_mode == 'cpu':
        prefetcher = CPUPrefetcher(train_loader)
    elif prefetch_mode == 'cuda':
        prefetcher = CUDAPrefetcher(train_loader, opt)
        logger.info(f'Use {prefetch_mode} prefetch dataloader')
        if opt['datasets']['train'].get('pin_memory') is not True:
            logger.warning('Please set pin_memory=True for CUDAPrefetcher.')
    else:
        raise ValueError(f'Wrong prefetch_mode {prefetch_mode}.'
                         "Supported ones are: None, 'cuda', 'cpu'.")

    for epoch in range(start_epoch, total_epochs + 1):
        if opt['dist']:
            train_sampler.set_epoch(epoch)
        prefetcher.reset()
        train_data = prefetcher.next()
        while train_data is not None:
            data_time = time.time() - data_time

            current_iter += 1
            if current_iter > total_iters:
                break
            # update learning rate
            model.update_learning_rate(
                current_iter, warmup_iter=opt['train']['warmup_iter'])
            # training
            model.feed_data(train_data)
            model.optimize_parameters(current_iter)
            iter_time = time.time() - iter_time
            # log
            if current_iter % opt['logger']['print_freq'] == 0:
                log_vars = {'epoch': epoch, 'iter': current_iter}
                log_vars.update({'lrs': model.get_current_learning_rate()})
                log_vars.update({'time': iter_time, 'data_time': data_time})
                log_vars.update(model.get_current_log())
                msg_logger(log_vars)

            # save models and training states
            if current_iter % opt['logger']['save_checkpoint_freq'] == 0:
                logger.info('Saving models and training states.')
                model.save(epoch, current_iter)

            # validation
            if opt['val']['val_freq'] is not None and current_iter % opt[
                    'val']['val_freq'] == 0:
                model.validation(val_loader, current_iter, tb_logger,
                                 opt['val']['save_img'])

            data_time = time.time()
            iter_time = time.time()
            train_data = prefetcher.next()
        # end of iter
    # end of epoch

    logger.info('End of training.')
    logger.info('Saving the latest model.')
    model.save(epoch=-1, current_iter=-1)  # -1 for the latest
    # last validation
    if opt['val']['val_freq'] is not None:
        model.validation(val_loader, current_iter, tb_logger,
                         opt['val']['save_img'])

    if tb_logger:
        tb_logger.close()


if __name__ == '__main__':
    main()
