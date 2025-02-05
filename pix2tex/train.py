from pix2tex.dataset.dataset import Im2LatexDataset
import os
import argparse
import logging
import yaml

import torch
from munch import Munch
from tqdm.auto import tqdm
import wandb

from pix2tex.eval import evaluate
from pix2tex.models import get_model
from pix2tex.utils import *


def train(args):
    dataloader = Im2LatexDataset().load(args.data)
    dataloader.update(**args, test=False)
    valdataloader = Im2LatexDataset().load(args.valdata)
    valargs = args.copy()
    valargs.update(batchsize=args.testbatchsize, keep_smaller_batches=True, test=True)
    valdataloader.update(**valargs)
    device = args.device
    model = get_model(args, training=True)
    if args.load_chkpt is not None:
        model.load_state_dict(torch.load(args.load_chkpt, map_location=device))
    encoder, decoder = model.encoder, model.decoder

    out_path = os.path.join(args.model_path, args.name)
    os.makedirs(out_path, exist_ok=True)

    def save_models(e):
        torch.save(model.state_dict(), os.path.join(out_path, '%s_e%02d.pth' % (args.name, e+1)))
        yaml.dump(dict(args), open(os.path.join(out_path, 'config.yaml'), 'w+'))

    opt = get_optimizer(args.optimizer)(model.parameters(), args.lr, betas=args.betas)
    scheduler = get_scheduler(args.scheduler)(opt, step_size=args.lr_step, gamma=args.gamma)
    try:
        for e in range(args.epoch, args.epochs):
            args.epoch = e
            dset = tqdm(iter(dataloader))
            for i, (seq, im) in enumerate(dset):
                if seq is not None and im is not None:
                    opt.zero_grad()
                    tgt_seq, tgt_mask = seq['input_ids'].to(device), seq['attention_mask'].bool().to(device)
                    encoded = encoder(im.to(device))
                    loss = decoder(tgt_seq, mask=tgt_mask, context=encoded)
                    loss.backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
                    opt.step()
                    scheduler.step()
                    dset.set_description('Loss: %.4f' % loss.item())
                    if args.wandb:
                        wandb.log({'train/loss': loss.item()})
                if (i+1) % args.sample_freq == 0:
                    evaluate(model, valdataloader, args, num_batches=int(args.valbatches*e/args.epochs), name='val')
            if (e+1) % args.save_freq == 0:
                save_models(e)
            if args.wandb:
                wandb.log({'train/epoch': e+1})
    except KeyboardInterrupt:
        if e >= 2:
            save_models(e)
        raise KeyboardInterrupt
    save_models(e)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train model')
    parser.add_argument('--config', default=None, help='path to yaml config file', type=str)
    parser.add_argument('--no_cuda', action='store_true', help='Use CPU')
    parser.add_argument('--debug', action='store_true', help='DEBUG')
    parser.add_argument('--resume', help='path to checkpoint folder', action='store_true')
    parsed_args = parser.parse_args()
    if parsed_args.config is None:
        with in_model_path():
            parsed_args.config = os.path.realpath('settings/debug.yaml')
    with open(parsed_args.config, 'r') as f:
        params = yaml.load(f, Loader=yaml.FullLoader)
    args = parse_args(Munch(params), **vars(parsed_args))
    logging.getLogger().setLevel(logging.DEBUG if parsed_args.debug else logging.WARNING)
    seed_everything(args.seed)
    if args.wandb:
        if not parsed_args.resume:
            args.id = wandb.util.generate_id()
        wandb.init(config=dict(args), resume='allow', name=args.name, id=args.id)
    train(args)
