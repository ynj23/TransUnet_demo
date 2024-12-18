# TransUnet代码
import torch
import torch.nn as nn
from einops import rearrange

from utils.vit import ViT
#定义下采样中的操作步骤（箭头）
class EncoderBottleneck(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1, base_width=64):
        super().__init__()
#下采样
        self.downsample = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride,  bias=False),
            nn.BatchNorm2d(out_channels),
        )

        width = int(out_channels * (base_width / 64))

        self.conv1 = nn.Conv2d(in_channels, width, kernel_size=1, stride=1, bias=False)
        self.norm1 = nn.BatchNorm2d(width)

        self.conv2 = nn.Conv2d(width, width, kernel_size=3, stride=2, padding=1, dilation=1, bias=False)
        self.norm2 = nn.BatchNorm2d(width)

        self.conv3 = nn.Conv2d(width, out_channels, kernel_size=1, stride=1, bias=False)
        self.norm3 = nn.BatchNorm2d(out_channels)

        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x_down = self.downsample(x)

        x = self.conv1(x)
        x = self.norm1(x)
        x = self.relu(x)

        x = self.conv2(x)
        x = self.norm2(x)
        x = self.relu(x)

        x = self.conv3(x)
        x = self.norm3(x)
        x = self.relu(x)

        x = x + x_down
        x = self.relu(x)
        return x
#反卷积上采样需要用到的小块
class DecoderBottleneck(nn.Module):
    def __init__(self, in_channels, out_channels, scale_factor=2):
        super().__init__()

        self.upsample = nn.Upsample(scale_factor=scale_factor, mode='bilinear', align_corners=True)
        self.layer = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, x, x_concat=None):
        x = self.upsample(x)

        if x_concat is not None:
            x = torch.cat( [x_concat, x], dim=1) # x有四个维度（batch_size, channels, height, width）

        x = self.layer(x)
        return x

#编码器部分
class Encoder(nn.Module):
    def __init__(self, img_dim, in_channels, out_channels, head_num, mlp_dim, block_num, patch_dim):
        super().__init__()
        #原始图像包括四个维度bchw,把卷积核大小取大一点是为了把hw尽可能减小，channels尽可能拉长，将图像变换成矩阵
        self.conv1 = nn.Conv2d(in_channels, out_channels, kernel_size=7, stride=2, padding=3, bias=False)
        #卷积完成后进行归一化处理
        self.norm1 = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)
#下采样卷积步骤
        self.encoder1 = EncoderBottleneck(out_channels, out_channels*2, stride=2)
        self.encoder2 = EncoderBottleneck(out_channels*2, out_channels*4, stride=2)
        self.encoder3 = EncoderBottleneck(out_channels*4, out_channels*8, stride=2)
#将提取的future map 放入Vit中
        self.vit_img_dim = img_dim // patch_dim #原来的图片尺寸卷积了几次
        self.vit = ViT(img_dim=self.vit_img_dim,
                       in_channels=out_channels*8,
                       embedding_dim=out_channels*8,
                       head_num=head_num,
                       mlp_dim=mlp_dim,
                       block_num=block_num,
                       patch_dim=1,
                       classification=False)
        #进行卷积操作
        self.conv2 = nn.Conv2d(out_channels*8, 512, kernel_size=3, stride=1, padding=1)
        self.norm2 = nn.BatchNorm2d(512)

    def forward(self, x):
        x = self.conv1(x)
        x = self.norm1(x)
        x1 = self.relu(x)#卷积结束后保存，方便后期反卷积的时候拼

        x2 = self.encoder1(x1)
        x3 = self.encoder2(x2)
        x = self.encoder3(x3)

        x = self.vit(x)
        #reshape过程，此时宽和高合在一个维度中，将其复原
        x = rearrange(x, "b (x y) c -> b c x y", x=self.vit_img_dim, y=self.vit_img_dim)

        x = self.conv2(x)
        x = self.norm2(x)
        x = self.relu(x)
        return x, x1, x2, x3
#decoder部分
class Decoder(nn.Module):
    #class_num表示语义分割的类别
    def __init__(self, out_channels, class_num):
        super().__init__()
#三次反卷积
        #需要有一个拼接的过程，所以将channels变为2，拼接之后就是4
        self.decoder1 = DecoderBottleneck(out_channels*8, out_channels*2)
        self.decoder2 = DecoderBottleneck(out_channels*4, out_channels)
        self.decoder3 = DecoderBottleneck(out_channels*2, int (out_channels*1 / 2))
        self.decoder4 = DecoderBottleneck(int (out_channels*1 / 2), int (out_channels*1 / 8))

        self.conv1 = nn.Conv2d(int (out_channels*1 / 8), class_num, kernel_size=1)

#最后一步从矩阵形式转换成图片形式
    def forward(self, x, x1, x2, x3):
        x = self.decoder1(x,x3)
        x = self.decoder2(x,x2)
        x = self.decoder3(x,x1)
        x = self.decoder4(x)
        x = self.conv1(x)
        return x

class TransUnet(nn.Module):
    def __init__(self, img_dim, in_channels, out_channels, head_num, mlp_dim, block_num, patch_dim, class_num):
        super().__init__()

        self.encoder = Encoder(img_dim, in_channels, out_channels, head_num, mlp_dim, block_num, patch_dim)
        self.decoder = Decoder(out_channels, class_num)

    def forward(self, x):
        x, x1, x2, x3 = self.encoder(x)
        x = self.decoder(x, x1, x2, x3)
        return x

if __name__ == '__main__':
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    transunet = TransUnet(
                            img_dim=128,
                            in_channels=3,
                            out_channels=128,
                            head_num=4,
                            mlp_dim=512,
                            block_num=8,
                            patch_dim=16,
                            class_num=1,).to(device)

    x = torch.rand(4, 3, 128, 128).to(device)

    pred = transunet(x)
    print("11")
    print(pred.shape)
