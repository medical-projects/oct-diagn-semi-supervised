import mlflow
import torch
from cortex.plugins import ModelPlugin
from cortex._lib import exp
from torch.backends import cudnn
import torch.nn.functional as F
import numpy as np
from torchsummary import summary
from torchvision.transforms import Resize

from src.models.mix_match.wideresnet import WideResNet
from src.models.mix_match.utils import interleave
from src.models.utils import accuracy, MlflowLogger


class MixMatchController(ModelPlugin):
    defaults = dict(
        data=dict(batch_size=dict(train=64, test=64), inputs=dict(inputs='images'), shuffle=True, skip_last_batch=True),
        train=dict(save_on_lowest='losses.classifier', epochs=1024*16, archive_every=1000),
        optimizer=dict(optimizer='Adam', learning_rate=0.002, single_optimizer=True)
    )

    # TODO Not exactly the same batches, as in MixMatch

    def optimizer_step(self, retain_graph=False):
        super().optimizer_step(retain_graph)
        self.ema_optimizer.step()

    def routine(self, T: float = 0.5, alpha: float = 0.75, *args, **kwargs):
        """
        :param alpha: Parameter of beta distribution
        :param T: Sharpening temperature
        """
        if self.data.mode == 'test':
            targets_l = self.inputs('data.targets')
            inputs_l = self.inputs('data.images')

            self.losses.classifier = torch.tensor(0.0).to(exp.DEVICE)

            with torch.no_grad():
                outputs_l = self.nets.ema_classifier(inputs_l)

        else:
            targets_l = self.inputs('data_l.targets')
            inputs_l = self.inputs('data_l.images')
            inputs_u1, inputs_u2 = self.inputs('data_u.images')

            # Transform label to one-hot
            targets_l_oh = torch.zeros(self.data.batch_size['train'], self.get_dims('data.targets'))
            targets_l_oh[range(targets_l_oh.shape[0]), targets_l] = 1.0
            targets_l_oh = targets_l_oh.to(exp.DEVICE)

            with torch.no_grad():
                # compute guessed labels of unlabel samples
                outputs_u1 = self.nets.classifier(inputs_u1)
                outputs_u2 = self.nets.classifier(inputs_u2)
                p = (torch.softmax(outputs_u1, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
                pt = p ** (1 / T)
                targets_u = pt / pt.sum(dim=1, keepdim=True)
                targets_u = targets_u.detach()

            # mixup
            all_inputs = torch.cat([inputs_l, inputs_u1, inputs_u2], dim=0)
            all_targets = torch.cat([targets_l_oh, targets_u, targets_u], dim=0)

            l = np.random.beta(alpha, alpha)
            l = max(l, 1 - l)

            idx = torch.randperm(all_inputs.size(0))
            input_a, input_b = all_inputs, all_inputs[idx]
            target_a, target_b = all_targets, all_targets[idx]

            mixed_input = l * input_a + (1 - l) * input_b
            mixed_target = l * target_a + (1 - l) * target_b

            # interleave labeled and unlabed samples between batches to get correct batchnorm calculation
            mixed_input = list(torch.split(mixed_input, self.data.batch_size['train']))
            mixed_input = interleave(mixed_input, self.data.batch_size['train'])

            logits = [self.nets.classifier(mixed_input[0])]
            for input in mixed_input[1:]:
                logits.append(self.nets.classifier(input))

            # put interleaved samples back
            logits = interleave(logits, self.data.batch_size['train'])
            logits_l = logits[0]
            logits_u = torch.cat(logits[1:], dim=0)

            Ll, Lu, w = self.loss(logits_l, mixed_target[:self.data.batch_size['train']],
                                  logits_u, mixed_target[self.data.batch_size['train']:],
                                  exp.INFO['epoch'] + exp.INFO['data_steps'] / exp.ARGS['train']['epochs'])

            # ema_optimizer.step()

            # record loss
            self.losses.classifier = Ll + w * Lu

            # Write res
            self.add_results(losses_l=Ll.item())
            self.add_results(losses_u=Lu.item())
            self.add_results(w=w)

            with torch.no_grad():
                outputs_l = self.nets.classifier(inputs_l)

        # Cross-entropy
        with torch.no_grad():
            cross_entropy = self.criterion(outputs_l, targets_l)
            self.add_results(cross_entropy=cross_entropy)

            # Top-k accuracy
            labeled = 1 - targets_l.eq(-1).long()
            top1 = accuracy(outputs_l, targets_l, labeled, top=1)
            # top5 = accuracy(outputs_l, targets_l, labeled, top=5)
            self.add_results(acc_top1=top1)

    def build(self, lambda_u: float = 75, ema_decay: float = 0.999, log_to_mlflow=True, *args, **kwargs):
        """
        :param log_to_mlflow: Log run to mlflow
        :param ema_decay: Exponential moving average decay rate
        :param lambda_u: Unlabeled loss weight
        """
        cudnn.benchmark = True

        # Reset the data iterator and draw batch to perform shape inference.
        self.data.reset(mode='test', make_pbar=False)
        self.data.next()
        input_shape = self.get_dims('data.images')

        self.nets.classifier = WideResNet(num_classes=self.get_dims('data.targets'))
        self.nets.ema_classifier = WideResNet(num_classes=self.get_dims('data.targets'))
        print(summary(self.nets.classifier, (1, 512, 512)))

        for param in self.nets.ema_classifier.parameters():
            param.detach_()

        self.ema_optimizer = WeightEMA(self.nets.classifier, self.nets.ema_classifier, alpha=ema_decay)
        self.loss = SemiLoss(lambda_u, exp.ARGS['train']['epochs'])
        self.criterion = torch.nn.CrossEntropyLoss()

        if log_to_mlflow:
            MlflowLogger.start_run(exp.INFO['name'] + '_MixMatch')
            MlflowLogger.log_basic_run_params(input_shape)
            MlflowLogger.log_ssl_parameters()
            mlflow.log_param('ema_decay', ema_decay)
            mlflow.log_param('lambda_u', lambda_u)
            mlflow.log_param('T', exp.ARGS['model']['T'])
            mlflow.log_param('alpha', exp.ARGS['model']['alpha'])

    def eval_loop(self):
        super().eval_loop()
        if MlflowLogger.log_to_mlflow:
            MlflowLogger.log_all_metrics(mode='train')
            MlflowLogger.log_all_metrics(mode='test')

    def visualize(self):
        inputs = self.inputs('data.images')
        targets = self.inputs('data.targets')

        self.add_image(F.adaptive_avg_pool2d(inputs, (64, 64)), name='Input', labels=targets)


class SemiLoss:
    def __init__(self, lambda_u, epochs):
        self.lambda_u = lambda_u
        self.epochs = epochs

    @staticmethod
    def linear_rampup(current, rampup_length):
        if rampup_length == 0:
            return 1.0
        else:
            current = np.clip(current / rampup_length, 0.0, 1.0)
            return float(current)

    def __call__(self, outputs_x, targets_x, outputs_u, targets_u, epoch):
        probs_u = torch.softmax(outputs_u, dim=1)

        Lx = -torch.mean(torch.sum(F.log_softmax(outputs_x, dim=1) * targets_x, dim=1))
        Lu = torch.mean((probs_u - targets_u) ** 2)

        return Lx, Lu, self.lambda_u * SemiLoss.linear_rampup(epoch, self.epochs)


class WeightEMA(object):
    def __init__(self, model, ema_model, alpha=0.999):
        self.model = model
        self.ema_model = ema_model
        self.alpha = alpha
        self.params = list(model.state_dict().values())
        self.ema_params = list(ema_model.state_dict().values())
        self.wd = 0.02 * exp.ARGS['optimizer']['learning_rate']

        for param, ema_param in zip(self.params, self.ema_params):
            param.data.copy_(ema_param.data)

    def step(self):
        one_minus_alpha = 1.0 - self.alpha
        for param, ema_param in zip(self.params, self.ema_params):
            if len(param.shape) > 0:
                ema_param.mul_(self.alpha)
                ema_param.add_(param * one_minus_alpha)
                # customized weight decay
                param.mul_(1 - self.wd)