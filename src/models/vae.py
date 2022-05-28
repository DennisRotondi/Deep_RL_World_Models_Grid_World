import torch
from torch import optim, nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import ReduceLROnPlateau
import wandb
import torchvision.utils
import pytorch_lightning as pl
from typing import Sequence, List, Dict, Tuple, Optional, Any, Set, Union, Callable, Mapping
#code based on https://github.com/ctallec/world-models and vae notebook, this is the structure illustrated in the paper

class Decoder(nn.Module):
    """ VAE decoder """
    def __init__(self, img_channels, latent_size):
        super(Decoder, self).__init__()
        self.latent_size = latent_size
        self.img_channels = img_channels

        self.fc1 = nn.Linear(latent_size, 1024)
        self.deconv1 = nn.ConvTranspose2d(1024, 128, 5, stride=2)
        self.deconv2 = nn.ConvTranspose2d(128, 64, 5, stride=2)
        self.deconv3 = nn.ConvTranspose2d(64, 32, 6, stride=2)
        self.deconv4 = nn.ConvTranspose2d(32, img_channels, 6, stride=2)

    def forward(self, x): # pylint: disable=arguments-differ
        x = F.relu(self.fc1(x))
        x = x.unsqueeze(-1).unsqueeze(-1)
        x = F.relu(self.deconv1(x))
        x = F.relu(self.deconv2(x))
        x = F.relu(self.deconv3(x))
        reconstruction = torch.sigmoid(self.deconv4(x))
        return reconstruction

class Encoder(nn.Module): # pylint: disable=too-many-instance-attributes
    """ VAE encoder """
    def __init__(self, img_channels, latent_size):
        super(Encoder, self).__init__()
        self.latent_size = latent_size
        self.img_channels = img_channels
        self.conv1 = nn.Conv2d(img_channels, 32, 4, stride=2)
        self.conv2 = nn.Conv2d(32, 64, 4, stride=2)
        self.conv3 = nn.Conv2d(64, 128, 4, stride=2)
        self.conv4 = nn.Conv2d(128, 256, 4, stride=2)
        self.fc_mu = nn.Linear(2*2*256, latent_size)
        self.fc_logsigma = nn.Linear(2*2*256, latent_size)

    def forward(self, x): 
        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x = F.relu(self.conv3(x))
        x = F.relu(self.conv4(x))
        x = x.view(x.size(0), -1)

        mu = self.fc_mu(x)
        logsigma = self.fc_logsigma(x)

        return mu, logsigma     

class VAE(pl.LightningModule):
    """ Variational Autoencoder """
    def __init__(self, hparams):
        super(VAE, self).__init__()
        self.save_hyperparameters(hparams)
        self.encoder = Encoder(self.hparams.img_channels, self.hparams.latent_size)
        self.decoder = Decoder(self.hparams.img_channels, self.hparams.latent_size)
        # It avoids wandb logging when lighting does a sanity check on the validation
        self.is_sanity = True

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        mu, logsigma = self.encoder(x)
        sigma = logsigma.exp()
        eps = torch.randn_like(sigma)
        z = eps.mul(sigma).add_(mu)
        x_recon = self.decoder(z)
        return x_recon, mu, logsigma

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=self.hparams.lr, betas=(0.9, 0.999), eps=1e-6, weight_decay=self.hparams.wd)
        reduce_lr_on_plateau = ReduceLROnPlateau(optimizer, mode='min',verbose=True, min_lr=1e-8)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": reduce_lr_on_plateau,
                "monitor": 'loss',
                "frequency": 1
            },
        }

    def loss_function(self,recon_x, x, mu, logsigma):
        """ VAE loss function """
        #original
        BCE = F.mse_loss(recon_x, x, reduction='sum')
        #from notebook
        #BCE = F.binary_cross_entropy(recon_x.view(-1, 12288), x.view(-1, 12288), reduction='sum')
        # see Appendix B from VAE paper:
        # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
        # https://arxiv.org/abs/1312.6114
        # (from notebook) You can look at the derivation of the KL term here https://arxiv.org/pdf/1907.08956.pdf
        #KLD = -0.5 * torch.sum(1 + logsigma - mu.pow(2) - logsigma.exp())
        # original
        KLD = -0.5 * torch.sum(1 + 2 * logsigma - mu.pow(2) - (2 * logsigma).exp())
        beta = 0.0001
        return {"loss": BCE + beta*KLD, "BCE": BCE, "KLD": KLD}

    def training_step(self, batch, batch_idx):
        obs = batch['obs']
        recon_batch, mu, logvar = self(obs)
        loss = self.loss_function(recon_batch, obs, mu, logvar)
        self.log_dict(loss)
        return loss['loss']

    # from cyclegan notebook
    def get_image_examples(self, real: torch.Tensor, reconstructed: torch.Tensor) -> Sequence[wandb.Image]:
        """
        Given real and "fake" translated images, produce a nice coupled images to log
        :param real: the real images
        :param reconstructed: the reconstructed image

        :returns: a sequence of wandb.Image to log and visualize the performance
        """
        example_images = []
        for i in range(real.shape[0]):
            couple = torchvision.utils.make_grid(
                [real[i], reconstructed[i]],
                nrow=2,
                normalize=True,
                scale_each=False,
                pad_value=1,
                padding=4,
            )
            example_images.append(
                wandb.Image(couple.permute(1, 2, 0).detach().cpu().numpy(), mode="RGB")# no need of .permute(1, 2, 0) since pil image
            )
        return example_images

    def validation_step(
        self, batch: Dict[str, torch.Tensor], batch_idx: int) -> Dict[str, Union[torch.Tensor,Sequence[wandb.Image]]]:
        obs = batch['obs']
        recon_batch, mu, logvar = self(obs)
        loss = self.loss_function(recon_batch, obs, mu, logvar)
        images = self.get_image_examples(obs, recon_batch)
        return {"loss_vae_val": loss['loss'], "images": images}

    def validation_epoch_end(
        self, outputs: List[Dict[str, torch.Tensor]]
    ) -> Dict[str, Union[torch.Tensor, Dict[str, Union[torch.Tensor,Sequence[wandb.Image]]]]]:
        """ Implements the behaviouir at the end of a validation epoch

        Currently it gathers all the produced examples and log them to wandb,
        limiting the logged examples to `hparams["log_images"]`.

        Then computes the mean of the losses and returns it. 
        Updates the progress bar label with this loss.

        :param outputs: a sequence that aggregates all the outputs of the validation steps

        :returns: the aggregated validation loss and information to update the progress bar
        """
        images = []

        for x in outputs:
            images.extend(x["images"])
            
        images = images[: self.hparams.log_images]

        if not self.is_sanity:  # ignore if it not a real validation epoch. The first one is not.
            print(f"Logged {len(images)} images for each category.")
            self.logger.experiment.log(
                {f"images": images},
                step=self.global_step,
            )
        self.is_sanity = False

        avg_loss = torch.stack([x["loss_vae_val"] for x in outputs]).mean()
        self.log_dict({"avg_val_loss_vae": avg_loss})
        return {"avg_val_loss_vae": avg_loss}