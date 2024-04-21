import os
from tempfile import gettempdir

import torch
from clearml import Task
from torch.utils.data import DataLoader
from tqdm import trange

from dataset.jsrt_dataset import JSRTDataset
from models.kpts_unet import KeypointUNet
from training.forward_func import forward_heatmap
from training.hyper_params import hp_parser
from kornia.augmentation import AugmentationSequential, RandomAffine

hp_parser.add_argument('--std', type=int, default=5, help='standard deviation in pixel for heatmap generation')
hp = hp_parser.parse_args()

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"

tags = []
use_data_aug = hp.rotate or hp.translate or hp.scale
if use_data_aug:
    tags.append('DataAug')

task = Task.init(project_name='DenseSeg', task_name='Heatmap Regression', tags=tags, auto_connect_frameworks=False,
                 auto_resource_monitoring=False,
                 auto_connect_arg_parser={'gpu_id': False, 'bce': False, 'supervision': False, 'reg_uv': False,
                                          'uv_loss': False, 'tv': False, 'uv_method': False})
# init pytorch
torch.manual_seed(hp.seed)
if hp.gpu_id is None and torch.cuda.is_available():  # workaround to enable GPU selection during HPO
    hp.gpu_id = 0
    Warning('GPU is available but not selected. Defaulting to GPU 0 to enable CUDA_VISIBLE_DEVICES selection.')
device = torch.device(f'cuda:{hp.gpu_id}' if torch.cuda.is_available() else 'cpu')

# define data loaders
dl_kwargs = {'num_workers': 4, 'pin_memory': True} if torch.cuda.is_available() else {}
train_dl = DataLoader(JSRTDataset('train', False), batch_size=hp.batch_size, drop_last=True, **dl_kwargs)
val_dl = DataLoader(JSRTDataset('test', False), batch_size=hp.infer_batch_size, shuffle=False, drop_last=False,
                    **dl_kwargs)

# define model
n_kpts = sum(train_dl.dataset.NUM_LANDMARKS.values())
model = KeypointUNet(n_kpts)
model.to(device)

optimizer = torch.optim.Adam(model.parameters(), lr=hp.lr)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=hp.epochs, eta_min=hp.lr / 100)

if use_data_aug:
    data_aug = AugmentationSequential(
        RandomAffine(degrees=hp.rotate, translate=(hp.translate,) * 2, scale=(1 - hp.scale, 1 + hp.scale), p=1),
        data_keys=['image', 'keypoints']
    )
else:
    data_aug = None

fwd_kwargs = {'model': model, 'optimizer': optimizer, 'device': device, 'data_aug': data_aug, 'std_pixel': hp.std}

for epoch in trange(hp.epochs, desc='training'):
    forward_heatmap('train', train_dl, epoch, **fwd_kwargs)
    forward_heatmap('val', val_dl, epoch, **fwd_kwargs)

    if hp.lr_scheduler:
        scheduler.step()
        # log learning rate
        task.get_logger().report_scalar(title='Learning rate', series='lr', value=scheduler.get_last_lr()[0],
                                        iteration=epoch)

# save model to ClearML
save_path = gettempdir() + '/kpts_unet.pth'
model.save(save_path)
task.update_output_model(save_path, model_name='final_model')
task.close()