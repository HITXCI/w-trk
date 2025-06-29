# w-trk
Partial source code of "E3-MOT: Enhanced End-to-End Multi-Object Tracking via Camera-LiDAR Sensor Fusion" submitted to The Visual Computer

# Getting Started
This repository includes the primary code for this paper: stage-1 and stage-2.
## environment setup for stage-1  
a.Env: Create a conda virtual environment
```
conda create -n MOT python=3.9  
conda activate MOT  
```
b.torch:Install torch and torchvision  
```
pip install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 --index-url https://download.pytorch.org/whl/cu118
```
c.GCC: make sure gcc>=5
```
# If gcc is not installed:
# conda install -c omgarcia gcc-6 # gcc-6.2

export PATH=YOUR_GCC_PATH/bin:$PATH
# Eg: export PATH=/mnt/gcc-5.4/bin:$PATH
```
d.CUDA: Before installing MMCV family, you need to set up the CUDA_HOME (for compiling some operators on the gpu).
```
export CUDA_HOME=YOUR_CUDA_PATH/
# Eg: export CUDA_HOME=/mnt/cuda-11.8/
```
e.Install mmcv-series packages.
```
git clone https://github.com/open-mmlab/mmcv.git & cd mmcv
git checkout v1.6.0
export MMCV_WITH_OPS=1 MMCV_CUDA_ARGS=-std=c++17
pip install -v -e .
pip install mmdet==2.26.0 mmsegmentation==0.29.1 mmdet3d==1.0.0rc6
```
f.Install stage-1
```
cd ~
git clone
```
