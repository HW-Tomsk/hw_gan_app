import os
import argparse
import logging
import numpy as np
# from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader
import torch
import torch.optim as optim
import torch.backends.cudnn as cudnn

import json

cudnn.benchmark = True

import matplotlib.pyplot as plt
# import cv2
# import utils
from models.progressive_gan import ProgressiveGAN as ProGAN
from models.model_module import FC_selu_first
from models import entropy

import anvil.server
import anvil.mpl_util

import sys

FACIES = 0
PORO = 1
PERM = 2

SMALL_SIZE = 6
plt.rc('font', size=SMALL_SIZE)
plt.rc('axes', titlesize=SMALL_SIZE)

seed = 42

alpha = 0.1
t_batch_size = 32
n_epochs = 30
lr = 5e-4

image_size = 128
image_depth = 3
num_filters = 128
nz = 30

archI = 'FC_selu_first'
netI = None
hidden_layer_size = 512
num_extra_layers = 2
nw = 30

os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

maximums = {}


def npy(x):
    return x.cpu().numpy()


def get_acc(imgs, ijs, vals):
    imgs = npy(imgs)
    ijs = npy(ijs)
    vals = npy(vals[:, 0])
    vals[vals < 0] = -1
    vals[vals > 0] = 1
    imgs[imgs > 0] = 1
    imgs[imgs < 0] = -1
    a = 0
    for img in imgs:
        if (img[0, ijs[:, 0], ijs[:, 1]] == vals).all():
            a += 1
            np.save('img_check', img)
            np.save('ijs_check', ijs)
            np.save('val_check', vals)
    return a / 32


class ModelManager:
    def __init__(self, name, pretrained_netI=False):
        """

        :param name: name of the model: object, sis or mps
        """
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.name = name
        self.maximums = {
            "sis": torch.tensor([1., 0.242726, 2.76592]).to(self.device),
            "object": torch.tensor([1., 0.22169, 2.41602]).to(self.device),
            "mps": torch.tensor([1., 0.261072, 3.07107]).to(self.device)
        }

        with open('files/{}.json'.format(self.name)) as json_file:
            data = json.load(json_file)
            self.means = torch.tensor(data['means']).to(self.device)
            self.stds = torch.tensor(data['stds']).to(self.device)

        model = ProGAN()
        model.load(path='files/{}.pt'.format(self.name), loadD=False)
        self.netG = model.netG

        for p in self.netG.parameters():
            p.requires_grad_(False)

        self.netG.eval()

        self.netI = FC_selu_first(input_size=nw, output_size=nz, hidden_layer_size=hidden_layer_size,
                                  num_extra_layers=num_extra_layers).to(self.device)
        if pretrained_netI:
            self.netI.load_state_dict(torch.load('files/netI_{}.pt'.format(self.name), map_location=torch.device('cpu')))
            self.netI.eval()

    def normalize(self, arr):
        arr = arr.T
        arr[:, 0] = (arr[:, 0] > 0.5)
        return (((arr / self.maximums[self.name]) - self.means) / self.stds).T

    def denormalize(self, arr):
        return ((arr * self.stds) + self.means) * self.maximums[self.name]

    def train(self, ijs, vals):
        """

        :param ijs: vector of shape (x, 2) with conditions' coodinates (max 128)
        :param vals: vector of shape(3, x) with conditions

        """
        writer = SummaryWriter()
        vals = torch.from_numpy(vals.astype(np.float32)).to(self.device)
        ij = torch.from_numpy(ijs).long().to(self.device)

        vals = self.normalize(vals)

        def logp(z):  # log posterior distribution
            x = self.netG(z)
            lpr = -0.5 * (z ** 2).view(z.shape[0], -1).sum(-1)  # log prior
            a = (x[..., ij[:, 0], ij[:, 1]] - vals) ** 2
            llh = -0.5 * a.view(x.shape[0], -1).sum(-1) / alpha  # log likelihood
            return llh + lpr / 5

        optimizer = optim.Adam(self.netI.parameters(), lr=lr, amsgrad=True, betas=(0.5, 0.9))
        w = torch.FloatTensor(t_batch_size, nw).to(self.device)

        for j in range(3000):
            optimizer.zero_grad()
            w.normal_(0, 1)
            z = self.netI(w)
            z = z.view(z.shape[0], z.shape[1], 1, 1)
            err = -logp(z).mean()
            ent = entropy.sample_entropy(z)
            kl = err - ent
            kl.backward()
            optimizer.step()
            if j % 5 == 0:
                with torch.no_grad():
                    writer.add_scalar("KL", kl.item(), j)
                    writer.add_scalar("ent", ent.item(), j)
                    writer.add_scalar("nlogp", err.item(), j)
                    # fig = plt.figure()
                    w.normal_(0, 1)
                    z = self.netI(w)
                    z = z.view(z.shape[0], z.shape[1], 1, 1)
                    a = self.netG(z).detach()
                    acc = get_acc(a, ij, vals.T)

                    writer.add_scalar("acc", acc, j)

    def predict(self, num, batch_size):
        with torch.no_grad():
            w = torch.FloatTensor(batch_size, nw).to(self.device)
            result = []
            second_result = []
            for i in range(int(num / batch_size)):
                w.normal_(0, 1)

                z = self.netI(w)
                z = z.view(z.shape[0], z.shape[1], 1, 1)

                output = self.netG(z).detach()
                second_result.extend(output)
                for img in output:
                    result.append(self.denormalize(img.permute(1, 2, 0)).T.cpu().numpy())

        return result, second_result


@anvil.server.callable
def get_sis():
    a, b = sis.predict(1, 1)

    f, axarr = plt.subplots(1, 3, dpi=400, tight_layout=True)

    facies_img = a[0][FACIES]
    facies_img[facies_img < 0.5] = 0
    facies_img[facies_img > 0.5] = 1

    poro_img = a[0][PORO]
    poro_img[facies_img == 0] = 0

    perm_img = a[0][PERM]
    perm_img[facies_img == 0] = 0

    a = axarr[0].matshow(facies_img, cmap=plt.get_cmap("gray"))
    axarr[0].set_xlabel("Фации")
    cbar1 = f.colorbar(a , ax=axarr[0], shrink=0.3, cmap='gray')
    show_dots(axarr[0])

    b = axarr[1].matshow(poro_img, cmap=plt.get_cmap("hot"))
    axarr[1].set_xlabel("Пористость")
    cbar2 = f.colorbar(b, ax=axarr[1], shrink=0.3, cmap='hot')
    show_dots(axarr[1])

    c = axarr[PERM].matshow(perm_img, cmap=plt.get_cmap("hot"))
    axarr[2].set_xlabel("Проницаемость")
    cbar3 = f.colorbar(c, ax=axarr[2], shrink=0.3, cmap='hot')
    show_dots(axarr[2])
    
    return anvil.mpl_util.plot_image()


@anvil.server.callable
def get_mps():
    a, b = mps.predict(1, 1)
    
    f, axarr = plt.subplots(1, 3, dpi=400, tight_layout=True)

    facies_img = a[0][FACIES]
    facies_img[facies_img < 0.5] = 0
    facies_img[facies_img > 0.5] = 1

    poro_img = a[0][PORO]
    poro_img[facies_img == 0] = 0

    perm_img = a[0][PERM]
    perm_img[facies_img == 0] = 0

    a = axarr[0].matshow(facies_img, cmap=plt.get_cmap("gray"))
    axarr[0].set_xlabel("Фации")
    cbar1 = f.colorbar(a , ax=axarr[0], shrink=0.3, cmap='gray')
    show_dots(axarr[0])

    b = axarr[1].matshow(poro_img, cmap=plt.get_cmap("hot"))
    axarr[1].set_xlabel("Пористость")
    cbar2 = f.colorbar(b, ax=axarr[1], shrink=0.3, cmap='hot')
    show_dots(axarr[1])

    c = axarr[PERM].matshow(perm_img, cmap=plt.get_cmap("hot"))
    axarr[2].set_xlabel("Проницаемость")
    cbar3 = f.colorbar(c, ax=axarr[2], shrink=0.3, cmap='hot')
    show_dots(axarr[2])

    return anvil.mpl_util.plot_image()


def show_dots(axs):
    idx_pos = np.where(vals[:, 0] < 0.5)
    idx_neg = np.where(vals[:, 0] > 0.5)

    axs.scatter(ijs[idx_pos][:, 0], ijs[idx_pos][:, 1], marker='+', color='green')
    axs.scatter(ijs[idx_neg][:, 0], ijs[idx_neg][:, 1], marker='*', color='blue')


@anvil.server.callable
def get_object():
    a, b = obj.predict(1, 1)

    f, axarr = plt.subplots(1, 3, dpi=400, tight_layout=True)

    facies_img = a[0][FACIES]
    facies_img[facies_img < 0.5] = 0
    facies_img[facies_img > 0.5] = 1

    poro_img = a[0][PORO]
    poro_img[facies_img == 0] = 0

    perm_img = a[0][PERM]
    perm_img[facies_img == 0] = 0

    a = axarr[0].matshow(facies_img, cmap=plt.get_cmap("gray"))
    axarr[0].set_xlabel("Фации")
    cbar1 = f.colorbar(a , ax=axarr[0], shrink=0.3, cmap='gray')
    show_dots(axarr[0])

    b = axarr[1].matshow(poro_img, cmap=plt.get_cmap("hot"))
    axarr[1].set_xlabel("Пористость")
    cbar2 = f.colorbar(b, ax=axarr[1], shrink=0.3, cmap='hot')
    show_dots(axarr[1])

    c = axarr[PERM].matshow(perm_img, cmap=plt.get_cmap("hot"))
    axarr[2].set_xlabel("Проницаемость")
    cbar3 = f.colorbar(c, ax=axarr[2], shrink=0.3, cmap='hot')
    show_dots(axarr[2])

    return anvil.mpl_util.plot_image()


def test():
    manager = ModelManager('sis')
    ijs = np.load('test_ijs.npy')
    vals = np.load('test_vals.npy').T
    manager.train(ijs, vals)
    a, b = manager.predict(16, 8)
    np.save('output', a)
    np.save('check', b)


ijs = np.load('test_ijs.npy')
vals = np.load('test_vals.npy')

sis = ModelManager('sis', pretrained_netI=True)
mps = ModelManager('mps', pretrained_netI=True)
obj = ModelManager('object', pretrained_netI=True)

if __name__ == '__main__':

    if len(sys.argv) < 2:
        sys.exit(1)

    while True:
        anvil.server.connect(sys.argv[1])

    # sis = ModelManager('sis')
    # mps = ModelManager('mps')
    # obj = ModelManager('object')
