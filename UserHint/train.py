import chainer
import chainer.functions as F
import numpy as np
import argparse

from chainer import cuda, serializers
from pathlib import Path
from model import Generator, Discriminator, VGG, SAGenerator
from utils import set_optimizer
from dataset import DataLoader, RefineDataset
from evaluation import Evaluation

xp = cuda.cupy
cuda.get_device(0).use()


class LossFunction:
    def __init__(self):
        pass

    @staticmethod
    def content_loss(y, t):
        return F.mean_absolute_error(y, t)

    @staticmethod
    def perceptual_loss(vgg, y, t):
        y_feat = vgg(y)
        t_feat = vgg(t)
        sum_loss = 0
        for yf, tf in zip(y_feat, t_feat):
            sum_loss += F.mean_squared_error(yf, tf)

        return sum_loss

    @staticmethod
    def dis_hinge_loss(y, t):
        return F.mean(F.relu(1. - t)) + F.mean(F.relu(1. + y))

    @staticmethod
    def gen_hinge_loss(y):
        return -F.mean(y)

    @staticmethod
    def positive_enforcing_loss(y):
        sum_loss = 0
        b, c, h, w = y.shape
        for color in range(3):
            ch = y[:, color, :, :]
            mean = F.mean(ch)
            mean = mean * chainer.as_variable(xp.ones(shape=(b, h, w)).astype(xp.float32))
            loss = F.mean_squared_error(ch, mean)
            sum_loss += loss

        return -sum_loss


def train(epochs, iterations, path, outdir, batchsize, validsize,
          adv_weight, enf_weight):
    # Dataset Definition
    dataloader = DataLoader(path)
    print(dataloader)
    color_valid, line_valid, mask_valid, ds_valid = dataloader(validsize, mode="valid")

    # Model & Optimizer Definition
    generator = SAGenerator(attn_type="sa")
    generator.to_gpu()
    gen_opt = set_optimizer(generator)

    discriminator = Discriminator()
    discriminator.to_gpu()
    dis_opt = set_optimizer(discriminator)

    vgg = VGG()
    vgg.to_gpu()
    vgg_opt = set_optimizer(vgg)
    vgg.base.disable_update()

    # Loss Function Definition
    lossfunc = LossFunction()

    # Evaluation Definition
    evaluator = Evaluation()

    for epoch in range(epochs):
        sum_loss = 0
        for batch in range(0, iterations, batchsize):
            color, line, mask, mask_ds = dataloader(batchsize)
            line_input = F.concat([line, mask])

            extractor = vgg(mask, extract=True)
            extractor = F.average_pooling_2d(extractor, 3, 2, 1)
            extractor.unchain_backward()

            fake = generator(line_input, mask_ds, extractor)
            y_dis = discriminator(fake, extractor)
            t_dis = discriminator(color, extractor)
            loss = adv_weight * lossfunc.dis_hinge_loss(y_dis, t_dis)

            fake.unchain_backward()

            discriminator.cleargrads()
            loss.backward()
            dis_opt.update()
            loss.unchain_backward()

            fake = generator(line_input, mask_ds, extractor)
            y_dis = discriminator(fake, extractor)

            loss = adv_weight * lossfunc.gen_hinge_loss(y_dis)
            loss += enf_weight * lossfunc.positive_enforcing_loss(fake)
            loss += lossfunc.content_loss(fake, color)

            generator.cleargrads()
            loss.backward()
            gen_opt.update()
            loss.unchain_backward()

            sum_loss += loss.data

            if batch == 0:
                serializers.save_npz(f"{outdir}/generator_{epoch}.model", generator)

                extractor = vgg(line_valid, extract=True)
                extractor = F.average_pooling_2d(extractor, 3, 2, 1)
                extractor.unchain_backward()
                line_valid_input = F.concat([line_valid, mask_valid])
                with chainer.using_config('train', False):
                    y_valid = generator(line_valid_input, ds_valid, extractor)
                y_valid = y_valid.data.get()
                c_valid = color_valid.data.get()
                input_valid = line_valid_input.data.get()

                evaluator(y_valid, c_valid, input_valid, outdir, epoch, validsize)

        print(f"epoch: {epoch}")
        print(f"loss: {sum_loss / iterations}")


def train_refine(epochs, iterations, path, outdir, batchsize, validsize,
                 adv_weight, enf_weight):
    # Dataset Definition
    dataloader = RefineDataset(path)
    print(dataloader)
    color_valid, line_valid, mask_valid, ds_valid, cm_valid = dataloader(validsize, mode="valid")

    # Model & Optimizer Definition
    generator = SAGenerator(attn_type="sa")
    generator.to_gpu()
    gen_opt = set_optimizer(generator)

    discriminator = Discriminator()
    discriminator.to_gpu()
    dis_opt = set_optimizer(discriminator)

    vgg = VGG()
    vgg.to_gpu()
    vgg_opt = set_optimizer(vgg)
    vgg.base.disable_update()

    # Loss Function Definition
    lossfunc = LossFunction()

    # Evaluation Definition
    evaluator = Evaluation()

    iteration = 0

    for epoch in range(epochs):
        sum_dis_loss = 0
        sum_gen_loss = 0
        for batch in range(0, iterations, batchsize):
            iteration += 1
            color, line, mask, mask_ds, color_mask = dataloader(batchsize)
            line_input = F.concat([line, mask])

            extractor = vgg(color_mask, extract=True)
            extractor = F.average_pooling_2d(extractor, 3, 2, 1)
            extractor.unchain_backward()

            fake = generator(line_input, mask_ds, extractor)
            y_dis = discriminator(fake, extractor)
            t_dis = discriminator(color, extractor)
            loss = adv_weight * lossfunc.dis_hinge_loss(y_dis, t_dis)

            fake.unchain_backward()

            discriminator.cleargrads()
            loss.backward()
            dis_opt.update()
            loss.unchain_backward()

            sum_dis_loss += loss.data

            fake = generator(line_input, mask_ds, extractor)
            y_dis = discriminator(fake, extractor)

            loss = adv_weight * lossfunc.gen_hinge_loss(y_dis)
            #loss += enf_weight * lossfunc.positive_enforcing_loss(fake)
            loss += lossfunc.content_loss(fake, color)

            generator.cleargrads()
            loss.backward()
            gen_opt.update()
            loss.unchain_backward()

            sum_gen_loss += loss.data

            if batch == 0:
                serializers.save_npz(f"{outdir}/generator_{epoch}.model", generator)

                extractor = vgg(cm_valid, extract=True)
                extractor = F.average_pooling_2d(extractor, 3, 2, 1)
                extractor.unchain_backward()
                line_valid_input = F.concat([line_valid, mask_valid])
                with chainer.using_config('train', False):
                    y_valid = generator(line_valid_input, ds_valid, extractor)
                y_valid = y_valid.data.get()
                c_valid = color_valid.data.get()
                input_valid = line_valid_input.data.get()
                cm_val = cm_valid.data.get()
                input_valid = np.concatenate([input_valid[:, 3:6], cm_val], axis=1)

                evaluator(y_valid, c_valid, input_valid, outdir, epoch, validsize)

            print(f"iter: {iteration} dis loss: {sum_dis_loss / iterations} gen loss: {sum_gen_loss / iterations}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAM")
    parser.add_argument('--e', type=int, default=1000, help="the number of epochs")
    parser.add_argument('--i', type=int, default=2000, help="the number of iterations")
    parser.add_argument('--b', type=int, default=8, help="batch size")
    parser.add_argument('--v', type=int, default=4, help="valid size")
    parser.add_argument('--enf', type=float, default=0.001, help="the weight of content loss")
    parser.add_argument('--a', type=float, default=0.01, help="the weight of adversarial loss")
    parser.add_argument('--s', type=str, default="normal", help="the type of training")

    args = parser.parse_args()

    dataset_path = Path('./danbooru-images/')
    outdir = Path('./outdir_refine/')
    outdir.mkdir(exist_ok=True)

    if args.s == "normal":
        train(args.e, args.i, dataset_path, outdir, args.b, args.v, args.a, args.enf)
    elif args.s == "refine":
        train_refine(args.e, args.i, dataset_path, outdir, args.b, args.v, args.a, args.enf)
    else:
        raise AttributeError
