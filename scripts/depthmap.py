# Author: thygate
# https://github.com/thygate/stable-diffusion-webui-depthmap-script

from operator import getitem
from pathlib import Path

from PIL import Image
from torchvision.transforms import Compose, transforms

from modules import shared, devices
from modules.images import get_next_sequence_number
from modules.shared import opts, cmd_opts

try:
    from tqdm import trange
except:
    from builtins import range as trange

import sys
import torch, gc
import cv2
import os.path
import numpy as np
import skimage.measure
import copy
import platform
import math
import traceback
import pathlib
import os

# Not sure if this is needed
try:
    script_dir = os.path.dirname(os.path.realpath(__file__))
    extension_dir = pathlib.Path(script_dir).parent
    sys.path.append(extension_dir)
except:
    sys.path.append('extensions/stable-diffusion-webui-depthmap-script')

# Ugly workaround to fix gradio tempfile issue
def ensure_gradio_temp_directory():
    try:
        import tempfile
        path = os.path.join(tempfile.gettempdir(), 'gradio')
        if not (os.path.exists(path)):
            os.mkdir(path)
    except Exception as e:
        traceback.print_exc()
ensure_gradio_temp_directory()

# Our code
from scripts.main import *
from scripts.stereoimage_generation import create_stereoimages

# midas imports
from dmidas.dpt_depth import DPTDepthModel
from dmidas.midas_net import MidasNet
from dmidas.midas_net_custom import MidasNet_small
from dmidas.transforms import Resize, NormalizeImage, PrepareForNet

# AdelaiDepth/LeReS imports
from lib.multi_depth_model_woauxi import RelDepthModel
from lib.net_tools import strip_prefix_if_present

# pix2pix/merge net imports
from pix2pix.options.test_options import TestOptions
from pix2pix.models.pix2pix4depth_model import Pix2Pix4DepthModel

# 3d-photo-inpainting imports
from inpaint.mesh import write_mesh, read_mesh, output_3d_photo
from inpaint.networks import Inpaint_Color_Net, Inpaint_Depth_Net, Inpaint_Edge_Net
from inpaint.utils import path_planning
from inpaint.bilateral_filtering import sparse_bilateral_filtering

# zoedepth
from dzoedepth.models.builder import build_model
from dzoedepth.utils.config import get_config
from dzoedepth.utils.misc import colorize
from dzoedepth.utils.geometry import depth_to_points, create_triangles

# TODO: next two should not be here
whole_size_threshold = 1600  # R_max from the paper
pix2pixsize = 1024

global video_mesh_data, video_mesh_fn
video_mesh_data = None
video_mesh_fn = None

class ModelHolder():
    def __init__(self):
        self.depth_model = None
        self.pix2pix_model = None
        self.depth_model_type = None
        self.device = None

        # Extra stuff
        self.resize_mode = None
        self.normalization = None

    def ensure_models(self, model_type, device: torch.device, boost: bool):
        # TODO: could make it more granular
        if model_type == -1 or model_type is None:
            self.unload_models()
            return
        # Certain optimisations are irreversible and not device-agnostic, thus changing device requires reloading
        if model_type != self.depth_model_type or boost != self.pix2pix_model is not None or device != self.device:
            self.unload_models()
            self.load_models(model_type, device, boost)

    def load_models(self, model_type, device: torch.device, boost: bool):
        """Ensure that the depth model is loaded"""
        # TODO: supply correct values for zoedepth
        net_width = 512
        net_height = 512

        # model path and name
        model_dir = "./models/midas"
        if model_type == 0:
            model_dir = "./models/leres"
        # create paths to model if not present
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs('./models/pix2pix', exist_ok=True)

        print("Loading model weights from ", end=" ")

        resize_mode = "minimal"
        normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

        # TODO: net_w, net_h
        model = None
        if model_type == 0:  # "res101"
            model_path = f"{model_dir}/res101.pth"
            print(model_path)
            ensure_file_downloaded(
                model_path,
                ["https://cloudstor.aarnet.edu.au/plus/s/lTIJF4vrvHCAI31/download",
                 "https://huggingface.co/lllyasviel/Annotators/resolve/5bc80eec2b4fddbb/res101.pth",
                 ],
                "1d696b2ef3e8336b057d0c15bc82d2fecef821bfebe5ef9d7671a5ec5dde520b")
            if device == torch.device('gpu'):
                checkpoint = torch.load(model_path)
            else:
                checkpoint = torch.load(model_path, map_location=torch.device('cpu'))
            model = RelDepthModel(backbone='resnext101')
            model.load_state_dict(strip_prefix_if_present(checkpoint['depth_model'], "module."), strict=True)
            del checkpoint
            devices.torch_gc()

        if model_type == 1:  # "dpt_beit_large_512" midas 3.1
            model_path = f"{model_dir}/dpt_beit_large_512.pt"
            print(model_path)
            ensure_file_downloaded(model_path,
                                   "https://github.com/isl-org/MiDaS/releases/download/v3_1/dpt_beit_large_512.pt")
            model = DPTDepthModel(
                path=model_path,
                backbone="beitl16_512",
                non_negative=True,
            )
            resize_mode = "minimal"
            normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

        if model_type == 2:  # "dpt_beit_large_384" midas 3.1
            model_path = f"{model_dir}/dpt_beit_large_384.pt"
            print(model_path)
            ensure_file_downloaded(model_path,
                                   "https://github.com/isl-org/MiDaS/releases/download/v3_1/dpt_beit_large_384.pt")
            model = DPTDepthModel(
                path=model_path,
                backbone="beitl16_384",
                non_negative=True,
            )
            resize_mode = "minimal"
            normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

        if model_type == 3:  # "dpt_large_384" midas 3.0
            model_path = f"{model_dir}/dpt_large-midas-2f21e586.pt"
            print(model_path)
            ensure_file_downloaded(model_path,
                                   "https://github.com/intel-isl/DPT/releases/download/1_0/dpt_large-midas-2f21e586.pt")
            model = DPTDepthModel(
                path=model_path,
                backbone="vitl16_384",
                non_negative=True,
            )
            resize_mode = "minimal"
            normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

        elif model_type == 4:  # "dpt_hybrid_384" midas 3.0
            model_path = f"{model_dir}/dpt_hybrid-midas-501f0c75.pt"
            print(model_path)
            ensure_file_downloaded(model_path,
                                   "https://github.com/intel-isl/DPT/releases/download/1_0/dpt_hybrid-midas-501f0c75.pt")
            model = DPTDepthModel(
                path=model_path,
                backbone="vitb_rn50_384",
                non_negative=True,
            )
            resize_mode = "minimal"
            normalization = NormalizeImage(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])

        elif model_type == 5:  # "midas_v21"
            model_path = f"{model_dir}/midas_v21-f6b98070.pt"
            print(model_path)
            ensure_file_downloaded(model_path,
                                   "https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21-f6b98070.pt")
            model = MidasNet(model_path, non_negative=True)
            resize_mode = "upper_bound"
            normalization = NormalizeImage(
                mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
            )

        elif model_type == 6:  # "midas_v21_small"
            model_path = f"{model_dir}/midas_v21_small-70d6b9c8.pt"
            print(model_path)
            ensure_file_downloaded(model_path,
                                   "https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21_small-70d6b9c8.pt")
            model = MidasNet_small(model_path, features=64, backbone="efficientnet_lite3", exportable=True,
                                   non_negative=True, blocks={'expand': True})
            resize_mode = "upper_bound"
            normalization = NormalizeImage(
                mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
            )

        elif model_type == 7:  # zoedepth_n
            print("zoedepth_n\n")
            conf = get_config("zoedepth", "infer")
            conf.img_size = [net_width, net_height]
            model = build_model(conf)

        elif model_type == 8:  # zoedepth_k
            print("zoedepth_k\n")
            conf = get_config("zoedepth", "infer", config_version="kitti")
            conf.img_size = [net_width, net_height]
            model = build_model(conf)

        elif model_type == 9:  # zoedepth_nk
            print("zoedepth_nk\n")
            conf = get_config("zoedepth_nk", "infer")
            conf.img_size = [net_width, net_height]
            model = build_model(conf)

        model.eval()  # prepare for evaluation
        # optimize
        if device == torch.device("cuda") and model_type in [0, 1, 2, 3, 4, 5, 6]:
            model = model.to(memory_format=torch.channels_last)
            if not cmd_opts.no_half and model_type != 0 and not boost:
                model = model.half()
        model.to(device)  # to correct device

        self.depth_model = model
        self.depth_model_type = model_type
        self.resize_mode = resize_mode
        self.normalization = normalization

        # load merge network if boost enabled or keepmodels enabled
        if boost or (hasattr(opts, 'depthmap_script_keepmodels') and opts.depthmap_script_keepmodels):
            # sfu.ca unfortunately is not very reliable, we use a mirror just in case
            ensure_file_downloaded(
                './models/pix2pix/latest_net_G.pth',
                ["https://huggingface.co/lllyasviel/Annotators/resolve/9a7d84251d487d11/latest_net_G.pth",
                 "https://sfu.ca/~yagiz/CVPR21/latest_net_G.pth"],
                '50ec735d74ed6499562d898f41b49343e521808b8dae589aa3c2f5c9ac9f7462')
            opt = TestOptions().parse()
            if device == torch.device('cpu'):
                opt.gpu_ids = []
            pix2pix_model = Pix2Pix4DepthModel(opt)
            pix2pix_model.save_dir = './models/pix2pix'
            pix2pix_model.load_networks('latest')
            pix2pix_model.eval()
            model.to(device)
            self.pix2pix_model = pix2pix_model

        devices.torch_gc()

    def get_default_net_size(self, model_type):
        # TODO: fill in, use in the GUI
        sizes = {
            1: [512, 512],
            2: [384, 384],
            3: [384, 384],
            4: [384, 384],
            5: [384, 384],
            6: [256, 256],
        }
        if model_type in sizes:
            return sizes[model_type]
        return [512, 512]

    def swap_to_cpu_memory(self):
        if self.depth_model is not None:
            self.depth_model.to(torch.device('cpu'))
        if self.pix2pix_model is not None:
            self.pix2pix_model.to(torch.device('cpu'))

    def unload_models(self):
        if self.depth_model is not None or self.pix2pix_model is not None:
            del self.depth_model
            self.depth_model = None
            del self.pix2pix_model
            self.pix2pix_model = None
            gc.collect()
            devices.torch_gc()

        self.depth_model_type = None
        self.deviceidx = None

    def get_raw_prediction(self, input, net_width, net_height):
        """Get prediction from the model currently loaded by the class.
        If boost is enabled, net_width and net_height will be ignored."""
        # input image
        img = cv2.cvtColor(np.asarray(input), cv2.COLOR_BGR2RGB) / 255.0
        # compute depthmap
        if not self.pix2pix_model != None:
            if self.depth_model_type == 0:
                raw_prediction = estimateleres(img, self.depth_model, net_width, net_height)
                raw_prediction_invert = True
            elif self.depth_model_type in [7, 8, 9]:
                raw_prediction = estimatezoedepth(input, self.depth_model, net_width, net_height)
                raw_prediction_invert = True
            else:
                raw_prediction = estimatemidas(img, self.depth_model, net_width, net_height,
                                               model_holder.resize_mode, model_holder.normalization)
                raw_prediction_invert = False
        else:
            raw_prediction = estimateboost(img, self.depth_model, self.depth_model_type, self.pix2pix_model)
            raw_prediction_invert = False
        return raw_prediction, raw_prediction_invert


model_holder = ModelHolder()

def convert_i16_to_rgb(image, like):
    # three channel, 8 bits per channel image
    output = np.zeros_like(like)
    output[:, :, 0] = image / 256.0
    output[:, :, 1] = image / 256.0
    output[:, :, 2] = image / 256.0
    return output


def unload_sd_model():
    if shared.sd_model is not None:
        shared.sd_model.cond_stage_model.to(devices.cpu)
        shared.sd_model.first_stage_model.to(devices.cpu)


def reload_sd_model():
    if shared.sd_model is not None:
        shared.sd_model.cond_stage_model.to(devices.device)
        shared.sd_model.first_stage_model.to(devices.device)


def run_depthmap(outpath, inputimages, inputdepthmaps, inputnames, inp):
    if len(inputimages) == 0 or inputimages[0] is None:
        return [], '', ''
    if len(inputdepthmaps) == 0:
        inputdepthmaps: list[Image] = [None for _ in range(len(inputimages))]
    inputdepthmaps_complete = all([x is not None for x in inputdepthmaps])

    background_removal = inp["background_removal"]
    background_removal_model = inp["background_removal_model"]
    boost = inp["boost"]
    clipdepth = inp["clipdepth"]
    clipthreshold_far = inp["clipthreshold_far"]
    clipthreshold_near = inp["clipthreshold_near"]
    combine_output = inp["combine_output"]
    combine_output_axis = inp["combine_output_axis"]
    depthmap_compute_device = inp["compute_device"]
    gen_mesh = inp["gen_mesh"]
    gen_normal = inp["gen_normal"] if "gen_normal" in inp else False
    gen_stereo = inp["gen_stereo"]
    inpaint = inp["inpaint"]
    inpaint_vids = inp["inpaint_vids"]
    invert_depth = inp["invert_depth"]
    match_size = inp["match_size"]
    mesh_occlude = inp["mesh_occlude"]
    mesh_spherical = inp["mesh_spherical"]
    model_type = inp["model_type"]
    net_height = inp["net_height"]
    net_width = inp["net_width"]
    pre_depth_background_removal = inp["pre_depth_background_removal"]
    save_background_removal_masks = inp["save_background_removal_masks"]
    output_depth = inp["output_depth"]
    show_heat = inp["show_heat"]
    stereo_balance = inp["stereo_balance"]
    stereo_divergence = inp["stereo_divergence"]
    stereo_fill = inp["stereo_fill"]
    stereo_modes = inp["stereo_modes"]
    stereo_separation = inp["stereo_separation"]

    # TODO: ideally, run_depthmap should not save meshes - that makes the function not pure
    print(f"\n{SCRIPT_NAME} {SCRIPT_VERSION} ({get_commit_hash()})")

    unload_sd_model()

    # TODO: this still should not be here
    background_removed_images = []
    # remove on base image before depth calculation
    if background_removal:
        if pre_depth_background_removal:
            inputimages = batched_background_removal(inputimages, background_removal_model)
            background_removed_images = inputimages
        else:
            background_removed_images = batched_background_removal(inputimages, background_removal_model)

    # init torch device
    global device
    if depthmap_compute_device == 'GPU' and not torch.cuda.is_available():
        print('WARNING: Cuda device was not found, cpu will be used')
        depthmap_compute_device = 'CPU'
    if depthmap_compute_device == 'GPU':
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")
    print("device: %s" % device)

    generated_images = [{} for _ in range(len(inputimages))]
    """Images that will be returned.
    Every array element corresponds to particular input image.
    Dictionary keys are types of images that were derived from the input image."""
    # TODO: ???
    meshsimple_fi = None
    inpaint_imgs = []
    inpaint_depths = []

    try:
        if not inputdepthmaps_complete:
            print("Loading model(s) ..")
            model_holder.ensure_models(model_type, device, boost)
        model = model_holder.depth_model
        pix2pix_model = model_holder.pix2pix_model

        print("Computing output(s) ..")
        # iterate over input images
        for count in trange(0, len(inputimages)):
            # override net size (size may be different for different images)
            if match_size:
                net_width, net_height = inputimages[count].width, inputimages[count].height

            # Convert single channel input (PIL) images to rgb
            if inputimages[count].mode == 'I':
                inputimages[count].point(lambda p: p * 0.0039063096, mode='RGB')
                inputimages[count] = inputimages[count].convert('RGB')

            raw_prediction = None
            """Raw prediction, as returned by a model. None if input depthmap is used."""
            raw_prediction_invert = False
            """True if near=dark on raw_prediction"""
            out = None
            if inputdepthmaps is not None and inputdepthmaps[count] is not None:
                # use custom depthmap
                dimg = inputdepthmaps[count]
                # resize if not same size as input
                if dimg.width != inputimages[count].width or dimg.height != inputimages[count].height:
                    dimg = dimg.resize((inputimages[count].width, inputimages[count].height), Image.Resampling.LANCZOS)

                if dimg.mode == 'I' or dimg.mode == 'P' or dimg.mode == 'L':
                    out = np.asarray(dimg, dtype="float")
                else:
                    out = np.asarray(dimg, dtype="float")[:, :, 0]
            else:
                raw_prediction, raw_prediction_invert = \
                    model_holder.get_raw_prediction(inputimages[count], net_width, net_height)

                # output
                if abs(raw_prediction.max() - raw_prediction.min()) > np.finfo("float").eps:
                    out = np.copy(raw_prediction)
                    # TODO: some models may output negative values, maybe these should be clamped to zero.
                    if raw_prediction_invert:
                        out *= -1
                    if clipdepth:
                        out = (out - out.min()) / (out.max() - out.min())  # normalize to [0; 1]
                        out = np.clip(out, clipthreshold_far, clipthreshold_near)
                else:
                    # Regretfully, the depthmap is broken and will be replaced with a black image
                    out = np.zeros(raw_prediction.shape)
            out = (out - out.min()) / (out.max() - out.min())  # normalize to [0; 1]

            # Single channel, 16 bit image. This loses some precision!
            # uint16 conversion uses round-down, therefore values should be [0; 2**16)
            numbytes = 2
            max_val = (2 ** (8 * numbytes))
            out = np.clip(out * max_val, 0, max_val - 0.1)  # Clipping form above is needed to avoid overflowing
            img_output = out.astype("uint16")
            """Depthmap (near=bright), as uint16"""

            # if 3dinpainting, store maps for processing in second pass
            if inpaint:
                inpaint_imgs.append(inputimages[count])
                inpaint_depths.append(img_output)

            # applying background masks after depth
            if background_removal:
                print('applying background masks')
                background_removed_image = background_removed_images[count]
                # maybe a threshold cut would be better on the line below.
                background_removed_array = np.array(background_removed_image)
                bg_mask = (background_removed_array[:, :, 0] == 0) & (background_removed_array[:, :, 1] == 0) & (
                            background_removed_array[:, :, 2] == 0) & (background_removed_array[:, :, 3] <= 0.2)
                img_output[bg_mask] = 0  # far value

                generated_images[count]['background_removed'] = background_removed_image

                if save_background_removal_masks:
                    bg_array = (1 - bg_mask.astype('int8')) * 255
                    mask_array = np.stack((bg_array, bg_array, bg_array, bg_array), axis=2)
                    mask_image = Image.fromarray(mask_array.astype(np.uint8))

                    generated_images[count]['foreground_mask'] = mask_image

            # A weird quirk: if user tries to save depthmap, whereas input depthmap is used,
            # depthmap will be outputed, even if combine_output is used.
            if output_depth and inputdepthmaps[count] is None:
                if output_depth:
                    img_depth = cv2.bitwise_not(img_output) if invert_depth else img_output
                    if combine_output:
                        img_concat = Image.fromarray(np.concatenate(
                            (inputimages[count], convert_i16_to_rgb(img_depth, inputimages[count])),
                            axis=combine_output_axis))
                        generated_images[count]['concat_depth'] = img_concat
                    else:
                        generated_images[count]['depth'] = Image.fromarray(img_depth)

            if show_heat:
                heatmap = colorize(img_output, cmap='inferno')
                generated_images[count]['heatmap'] = heatmap

            if gen_stereo:
                print("Generating stereoscopic images..")
                stereoimages = create_stereoimages(inputimages[count], img_output, stereo_divergence, stereo_separation,
                                                   stereo_modes, stereo_balance, stereo_fill)
                for c in range(0, len(stereoimages)):
                    generated_images[count][stereo_modes[c]] = stereoimages[c]

            if gen_normal:  # TODO: should be moved into a separate file when redesigned
                # taken from @graemeniedermayer
                # take gradients
                zx = cv2.Sobel(np.float64(img_output), cv2.CV_64F, 1, 0, ksize=3)  # TODO: CV_64F ?
                zy = cv2.Sobel(np.float64(img_output), cv2.CV_64F, 0, 1, ksize=3)

                # combine and normalize gradients.
                normal = np.dstack((zx, -zy, np.ones_like(img_output)))
                n = np.linalg.norm(normal, axis=2)
                normal[:, :, 0] /= n
                normal[:, :, 1] /= n
                normal[:, :, 2] /= n

                # offset and rescale values to be in 0-255
                normal += 1
                normal /= 2
                normal *= 255
                normal = normal.astype(np.uint8)

                generated_images[count]['normal'] = Image.fromarray(normal)

            # gen mesh
            if gen_mesh:
                print(f"\nGenerating (occluded) mesh ..")
                basename = 'depthmap'
                meshsimple_fi = get_uniquefn(outpath, basename, 'obj')
                meshsimple_fi = os.path.join(outpath, meshsimple_fi + '_simple.obj')

                depthi = raw_prediction if raw_prediction is not None else out
                depthi_min, depthi_max = depthi.min(), depthi.max()
                # try to map output to sensible values for non zoedepth models, boost, or custom maps
                if model_type not in [7, 8, 9] or boost or inputdepthmaps[count] is not None:
                    # invert if midas
                    if model_type > 0 or inputdepthmaps[count] is not None:  # TODO: Weird
                        depthi = depthi_max - depthi + depthi_min
                        depth_max = depthi.max()
                        depth_min = depthi.min()
                    # make positive
                    if depthi_min < 0:
                        depthi = depthi - depthi_min
                        depth_max = depthi.max()
                        depth_min = depthi.min()
                    # scale down
                    if depthi.max() > 10.0:
                        depthi = 4.0 * (depthi - depthi_min) / (depthi_max - depthi_min)
                    # offset
                    depthi = depthi + 1.0

                mesh = create_mesh(inputimages[count], depthi, keep_edges=not mesh_occlude, spherical=mesh_spherical)
                mesh.export(meshsimple_fi)

        print("Computing output(s) done.")
    except RuntimeError as e:
        # TODO: display in UI
        if 'out of memory' in str(e):
            print("ERROR: out of memory, could not generate depthmap !")
        else:
            print(e)
    finally:
        if not (hasattr(opts, 'depthmap_script_keepmodels') and opts.depthmap_script_keepmodels):
            if 'model' in locals():
                del model
            if boost and 'pix2pixmodel' in locals():
                del pix2pix_model
            model_holder.unload_models()
        else:
            model_holder.swap_to_cpu_memory()

        gc.collect()
        devices.torch_gc()

    # TODO: This should not be here
    mesh_fi = None
    if inpaint:
        try:
            mesh_fi = run_3dphoto(device, inpaint_imgs, inpaint_depths, inputnames, outpath, inpaint_vids, 1, "mp4")
        except Exception as e:
            print(f'{str(e)}, some issue with generating inpainted mesh')

    reload_sd_model()
    print("All done.")
    return generated_images, mesh_fi, meshsimple_fi


def get_uniquefn(outpath, basename, ext):
    # Inefficient and may fail, maybe use unbounded binary search?
    basecount = get_next_sequence_number(outpath, basename)
    if basecount > 0: basecount = basecount - 1
    fullfn = None
    for i in range(500):
        fn = f"{basecount + i:05}" if basename == '' else f"{basename}-{basecount + i:04}"
        fullfn = os.path.join(outpath, f"{fn}.{ext}")
        if not os.path.exists(fullfn):
            break
    basename = Path(fullfn).stem

    return basename


def run_3dphoto(device, img_rgb, img_depth, inputnames, outpath, inpaint_vids, vid_ssaa, vid_format):
    mesh_fi = ''
    try:
        print("Running 3D Photo Inpainting .. ")
        edgemodel_path = './models/3dphoto/edge_model.pth'
        depthmodel_path = './models/3dphoto/depth_model.pth'
        colormodel_path = './models/3dphoto/color_model.pth'
        # create paths to model if not present
        os.makedirs('./models/3dphoto/', exist_ok=True)

        ensure_file_downloaded(edgemodel_path,
                               "https://filebox.ece.vt.edu/~jbhuang/project/3DPhoto/model/edge-model.pth")
        ensure_file_downloaded(depthmodel_path,
                               "https://filebox.ece.vt.edu/~jbhuang/project/3DPhoto/model/depth-model.pth")
        ensure_file_downloaded(colormodel_path,
                               "https://filebox.ece.vt.edu/~jbhuang/project/3DPhoto/model/color-model.pth")

        print("Loading edge model ..")
        depth_edge_model = Inpaint_Edge_Net(init_weights=True)
        depth_edge_weight = torch.load(edgemodel_path, map_location=torch.device(device))
        depth_edge_model.load_state_dict(depth_edge_weight)
        depth_edge_model = depth_edge_model.to(device)
        depth_edge_model.eval()
        print("Loading depth model ..")
        depth_feat_model = Inpaint_Depth_Net()
        depth_feat_weight = torch.load(depthmodel_path, map_location=torch.device(device))
        depth_feat_model.load_state_dict(depth_feat_weight, strict=True)
        depth_feat_model = depth_feat_model.to(device)
        depth_feat_model.eval()
        depth_feat_model = depth_feat_model.to(device)
        print("Loading rgb model ..")
        rgb_model = Inpaint_Color_Net()
        rgb_feat_weight = torch.load(colormodel_path, map_location=torch.device(device))
        rgb_model.load_state_dict(rgb_feat_weight)
        rgb_model.eval()
        rgb_model = rgb_model.to(device)

        config = {}
        config["gpu_ids"] = 0
        config['extrapolation_thickness'] = 60
        config['extrapolate_border'] = True
        config['depth_threshold'] = 0.04
        config['redundant_number'] = 12
        config['ext_edge_threshold'] = 0.002
        config['background_thickness'] = 70
        config['context_thickness'] = 140
        config['background_thickness_2'] = 70
        config['context_thickness_2'] = 70
        config['log_depth'] = True
        config['depth_edge_dilate'] = 10
        config['depth_edge_dilate_2'] = 5
        config['largest_size'] = 512
        config['repeat_inpaint_edge'] = True
        config['ply_fmt'] = "bin"

        config['save_ply'] = False
        if hasattr(opts, 'depthmap_script_save_ply') and opts.depthmap_script_save_ply:
            config['save_ply'] = True

        config['save_obj'] = True

        if device == torch.device("cpu"):
            config["gpu_ids"] = -1

        for count in trange(0, len(img_rgb)):
            basename = 'depthmap'
            if inputnames is not None:
                if inputnames[count] is not None:
                    p = Path(inputnames[count])
                    basename = p.stem

            basename = get_uniquefn(outpath, basename, 'obj')
            mesh_fi = os.path.join(outpath, basename + '.obj')

            print(f"\nGenerating inpainted mesh .. (go make some coffee) ..")

            # from inpaint.utils.get_MiDaS_samples
            W = img_rgb[count].width
            H = img_rgb[count].height
            int_mtx = np.array([[max(H, W), 0, W // 2], [0, max(H, W), H // 2], [0, 0, 1]]).astype(np.float32)
            if int_mtx.max() > 1:
                int_mtx[0, :] = int_mtx[0, :] / float(W)
                int_mtx[1, :] = int_mtx[1, :] / float(H)

            # how inpaint.utils.read_MiDaS_depth() imports depthmap
            disp = img_depth[count].astype(np.float32)
            disp = disp - disp.min()
            disp = cv2.blur(disp / disp.max(), ksize=(3, 3)) * disp.max()
            disp = (disp / disp.max()) * 3.0
            depth = 1. / np.maximum(disp, 0.05)

            # rgb input
            img = np.asarray(img_rgb[count])

            # run sparse bilateral filter
            config['sparse_iter'] = 5
            config['filter_size'] = [7, 7, 5, 5, 5]
            config['sigma_s'] = 4.0
            config['sigma_r'] = 0.5
            vis_photos, vis_depths = sparse_bilateral_filtering(depth.copy(), img.copy(), config,
                                                                num_iter=config['sparse_iter'], spdb=False)
            depth = vis_depths[-1]

            # bilat_fn = os.path.join(outpath, basename +'_bilatdepth.png')
            # cv2.imwrite(bilat_fn, depth)

            rt_info = write_mesh(img,
                                 depth,
                                 int_mtx,
                                 mesh_fi,
                                 config,
                                 rgb_model,
                                 depth_edge_model,
                                 depth_edge_model,
                                 depth_feat_model)

            if rt_info is not False and inpaint_vids:
                run_3dphoto_videos(mesh_fi, basename, outpath, 300, 40,
                                   [0.03, 0.03, 0.05, 0.03],
                                   ['double-straight-line', 'double-straight-line', 'circle', 'circle'],
                                   [0.00, 0.00, -0.015, -0.015],
                                   [0.00, 0.00, -0.015, -0.00],
                                   [-0.05, -0.05, -0.05, -0.05],
                                   ['dolly-zoom-in', 'zoom-in', 'circle', 'swing'], False, vid_format, vid_ssaa)

            devices.torch_gc()

    finally:
        del rgb_model
        rgb_model = None
        del depth_edge_model
        depth_edge_model = None
        del depth_feat_model
        depth_feat_model = None
        devices.torch_gc()

    return mesh_fi


def run_3dphoto_videos(mesh_fi, basename, outpath, num_frames, fps, crop_border, traj_types, x_shift_range,
                       y_shift_range, z_shift_range, video_postfix, vid_dolly, vid_format, vid_ssaa):
    import vispy
    if platform.system() == 'Windows':
        vispy.use(app='PyQt5')
    elif platform.system() == 'Darwin':
        vispy.use('PyQt6')
    else:
        vispy.use(app='egl')

    # read ply
    global video_mesh_data, video_mesh_fn
    if video_mesh_fn is None or video_mesh_fn != mesh_fi:
        del video_mesh_data
        video_mesh_fn = mesh_fi
        video_mesh_data = read_mesh(mesh_fi)

    verts, colors, faces, Height, Width, hFov, vFov, mean_loc_depth = video_mesh_data

    original_w = output_w = W = Width
    original_h = output_h = H = Height
    int_mtx = np.array([[max(H, W), 0, W // 2], [0, max(H, W), H // 2], [0, 0, 1]]).astype(np.float32)
    if int_mtx.max() > 1:
        int_mtx[0, :] = int_mtx[0, :] / float(W)
        int_mtx[1, :] = int_mtx[1, :] / float(H)

    config = {}
    config['video_folder'] = outpath
    config['num_frames'] = num_frames
    config['fps'] = fps
    config['crop_border'] = crop_border
    config['traj_types'] = traj_types
    config['x_shift_range'] = x_shift_range
    config['y_shift_range'] = y_shift_range
    config['z_shift_range'] = z_shift_range
    config['video_postfix'] = video_postfix
    config['ssaa'] = vid_ssaa

    # from inpaint.utils.get_MiDaS_samples
    generic_pose = np.eye(4)
    assert len(config['traj_types']) == len(config['x_shift_range']) == \
           len(config['y_shift_range']) == len(config['z_shift_range']) == len(config['video_postfix']), \
        "The number of elements in 'traj_types', 'x_shift_range', 'y_shift_range', 'z_shift_range' and \
            'video_postfix' should be equal."
    tgt_pose = [[generic_pose * 1]]
    tgts_poses = []
    for traj_idx in range(len(config['traj_types'])):
        tgt_poses = []
        sx, sy, sz = path_planning(config['num_frames'], config['x_shift_range'][traj_idx],
                                   config['y_shift_range'][traj_idx],
                                   config['z_shift_range'][traj_idx], path_type=config['traj_types'][traj_idx])
        for xx, yy, zz in zip(sx, sy, sz):
            tgt_poses.append(generic_pose * 1.)
            tgt_poses[-1][:3, -1] = np.array([xx, yy, zz])
        tgts_poses += [tgt_poses]
    tgt_pose = generic_pose * 1

    # seems we only need the depthmap to calc mean_loc_depth, which is only used when doing 'dolly'
    # width and height are already in the ply file in the comments ..
    # might try to add the mean_loc_depth to it too
    # did just that
    # mean_loc_depth = img_depth[img_depth.shape[0]//2, img_depth.shape[1]//2]

    print("Generating videos ..")

    normal_canvas, all_canvas = None, None
    videos_poses, video_basename = copy.deepcopy(tgts_poses), basename
    top = (original_h // 2 - int_mtx[1, 2] * output_h)
    left = (original_w // 2 - int_mtx[0, 2] * output_w)
    down, right = top + output_h, left + output_w
    border = [int(xx) for xx in [top, down, left, right]]
    normal_canvas, all_canvas, fn_saved = output_3d_photo(verts.copy(), colors.copy(), faces.copy(),
                                                          copy.deepcopy(Height), copy.deepcopy(Width),
                                                          copy.deepcopy(hFov), copy.deepcopy(vFov),
                                                          copy.deepcopy(tgt_pose), config['video_postfix'],
                                                          copy.deepcopy(generic_pose),
                                                          copy.deepcopy(config['video_folder']),
                                                          None, copy.deepcopy(int_mtx), config, None,
                                                          videos_poses, video_basename, original_h, original_w,
                                                          border=border, depth=None, normal_canvas=normal_canvas,
                                                          all_canvas=all_canvas,
                                                          mean_loc_depth=mean_loc_depth, dolly=vid_dolly, fnExt=vid_format)
    return fn_saved


# called from gen vid tab button
def run_makevideo(fn_mesh, vid_numframes, vid_fps, vid_traj, vid_shift, vid_border, dolly, vid_format, vid_ssaa):
    if len(fn_mesh) == 0 or not os.path.exists(fn_mesh):
        raise Exception("Could not open mesh.")

    vid_ssaa = int(vid_ssaa)

    # traj type
    if vid_traj == 0:
        vid_traj = ['straight-line']
    elif vid_traj == 1:
        vid_traj = ['double-straight-line']
    elif vid_traj == 2:
        vid_traj = ['circle']

    num_fps = int(vid_fps)
    num_frames = int(vid_numframes)
    shifts = vid_shift.split(',')
    if len(shifts) != 3:
        raise Exception("Translate requires 3 elements.")
    x_shift_range = [float(shifts[0])]
    y_shift_range = [float(shifts[1])]
    z_shift_range = [float(shifts[2])]

    borders = vid_border.split(',')
    if len(borders) != 4:
        raise Exception("Crop Border requires 4 elements.")
    crop_border = [float(borders[0]), float(borders[1]), float(borders[2]), float(borders[3])]

    # output path and filename mess ..
    basename = Path(fn_mesh).stem
    outpath = opts.outdir_samples or opts.outdir_extras_samples
    # unique filename
    basecount = get_next_sequence_number(outpath, basename)
    if basecount > 0: basecount = basecount - 1
    fullfn = None
    for i in range(500):
        fn = f"{basecount + i:05}" if basename == '' else f"{basename}-{basecount + i:04}"
        fullfn = os.path.join(outpath, f"{fn}_." + vid_format)
        if not os.path.exists(fullfn):
            break
    basename = Path(fullfn).stem
    basename = basename[:-1]

    print("Loading mesh ..")

    fn_saved = run_3dphoto_videos(fn_mesh, basename, outpath, num_frames, num_fps, crop_border, vid_traj, x_shift_range,
                                  y_shift_range, z_shift_range, [''], dolly, vid_format, vid_ssaa)

    return fn_saved[-1], fn_saved[-1], ''


def unload_models():
    model_holder.unload_models()


# TODO: code borrowed from the internet to be marked as such and to reside in separate files

def batched_background_removal(inimages, model_name):
    from rembg import new_session, remove
    print('creating background masks')
    outimages = []

    # model path and name
    bg_model_dir = Path.joinpath(Path().resolve(), "models/rem_bg")
    os.makedirs(bg_model_dir, exist_ok=True)
    os.environ["U2NET_HOME"] = str(bg_model_dir)

    # starting a session
    background_removal_session = new_session(model_name)
    for count in range(0, len(inimages)):
        bg_remove_img = np.array(remove(inimages[count], session=background_removal_session))
        outimages.append(Image.fromarray(bg_remove_img))
    # The line below might be redundant
    del background_removal_session
    return outimages


def ensure_file_downloaded(filename, url, sha256_hash_prefix=None):
    # Do not check the hash every time - it is somewhat time-consuming
    if os.path.exists(filename):
        return

    if type(url) is not list:
        url = [url]
    for cur_url in url:
        try:
            print("Downloading", cur_url, "to", filename)
            torch.hub.download_url_to_file(cur_url, filename, sha256_hash_prefix)
            if os.path.exists(filename):
                return  # The correct model was downloaded, no need to try more
        except:
            pass
    raise RuntimeError('Download failed. Try again later or manually download the file to that location.')


def estimatezoedepth(img, model, w, h):
    # x = transforms.ToTensor()(img).unsqueeze(0)
    # x = x.type(torch.float32)
    # x.to(device)
    # prediction = model.infer(x)
    model.core.prep.resizer._Resize__width = w
    model.core.prep.resizer._Resize__height = h
    prediction = model.infer_pil(img)

    return prediction


def scale_torch(img):
    """
    Scale the image and output it in torch.tensor.
    :param img: input rgb is in shape [H, W, C], input depth/disp is in shape [H, W]
    :param scale: the scale factor. float
    :return: img. [C, H, W]
    """
    if len(img.shape) == 2:
        img = img[np.newaxis, :, :]
    if img.shape[2] == 3:
        transform = transforms.Compose(
            [transforms.ToTensor(), transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))])
        img = transform(img.astype(np.float32))
    else:
        img = img.astype(np.float32)
        img = torch.from_numpy(img)
    return img


def estimateleres(img, model, w, h):
    # leres transform input
    rgb_c = img[:, :, ::-1].copy()
    A_resize = cv2.resize(rgb_c, (w, h))
    img_torch = scale_torch(A_resize)[None, :, :, :]

    # compute
    with torch.no_grad():
        if device == torch.device("cuda"):
            img_torch = img_torch.cuda()
        prediction = model.depth_model(img_torch)

    prediction = prediction.squeeze().cpu().numpy()
    prediction = cv2.resize(prediction, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_CUBIC)

    return prediction


def estimatemidas(img, model, w, h, resize_mode, normalization):
    import contextlib
    # init transform
    transform = Compose(
        [
            Resize(
                w,
                h,
                resize_target=None,
                keep_aspect_ratio=True,
                ensure_multiple_of=32,
                resize_method=resize_mode,
                image_interpolation_method=cv2.INTER_CUBIC,
            ),
            normalization,
            PrepareForNet(),
        ]
    )

    # transform input
    img_input = transform({"image": img})["image"]

    # compute
    precision_scope = torch.autocast if shared.cmd_opts.precision == "autocast" and device == torch.device(
        "cuda") else contextlib.nullcontext
    with torch.no_grad(), precision_scope("cuda"):
        sample = torch.from_numpy(img_input).to(device).unsqueeze(0)
        if device == torch.device("cuda"):
            sample = sample.to(memory_format=torch.channels_last)
            if not cmd_opts.no_half:
                sample = sample.half()
        prediction = model.forward(sample)
        prediction = (
            torch.nn.functional.interpolate(
                prediction.unsqueeze(1),
                size=img.shape[:2],
                mode="bicubic",
                align_corners=False,
            )
            .squeeze()
            .cpu()
            .numpy()
        )

    return prediction


def estimatemidasBoost(img, model, w, h):
    # init transform
    transform = Compose(
        [
            Resize(
                w,
                h,
                resize_target=None,
                keep_aspect_ratio=True,
                ensure_multiple_of=32,
                resize_method="upper_bound",
                image_interpolation_method=cv2.INTER_CUBIC,
            ),
            NormalizeImage(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            PrepareForNet(),
        ]
    )

    # transform input
    img_input = transform({"image": img})["image"]

    # compute
    with torch.no_grad():
        sample = torch.from_numpy(img_input).to(device).unsqueeze(0)
        if device == torch.device("cuda"):
            sample = sample.to(memory_format=torch.channels_last)
        prediction = model.forward(sample)

    prediction = prediction.squeeze().cpu().numpy()
    prediction = cv2.resize(prediction, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_CUBIC)

    # normalization
    depth_min = prediction.min()
    depth_max = prediction.max()

    if depth_max - depth_min > np.finfo("float").eps:
        prediction = (prediction - depth_min) / (depth_max - depth_min)
    else:
        prediction = 0

    return prediction


def generatemask(size):
    # Generates a Guassian mask
    mask = np.zeros(size, dtype=np.float32)
    sigma = int(size[0] / 16)
    k_size = int(2 * np.ceil(2 * int(size[0] / 16)) + 1)
    mask[int(0.15 * size[0]):size[0] - int(0.15 * size[0]), int(0.15 * size[1]): size[1] - int(0.15 * size[1])] = 1
    mask = cv2.GaussianBlur(mask, (int(k_size), int(k_size)), sigma)
    mask = (mask - mask.min()) / (mask.max() - mask.min())
    mask = mask.astype(np.float32)
    return mask


def resizewithpool(img, size):
    i_size = img.shape[0]
    n = int(np.floor(i_size / size))

    out = skimage.measure.block_reduce(img, (n, n), np.max)
    return out


def rgb2gray(rgb):
    # Converts rgb to gray
    return np.dot(rgb[..., :3], [0.2989, 0.5870, 0.1140])


def calculateprocessingres(img, basesize, confidence=0.1, scale_threshold=3, whole_size_threshold=3000):
    # Returns the R_x resolution described in section 5 of the main paper.

    # Parameters:
    #    img :input rgb image
    #    basesize : size the dilation kernel which is equal to receptive field of the network.
    #    confidence: value of x in R_x; allowed percentage of pixels that are not getting any contextual cue.
    #    scale_threshold: maximum allowed upscaling on the input image ; it has been set to 3.
    #    whole_size_threshold: maximum allowed resolution. (R_max from section 6 of the main paper)

    # Returns:
    #    outputsize_scale*speed_scale :The computed R_x resolution
    #    patch_scale: K parameter from section 6 of the paper

    # speed scale parameter is to process every image in a smaller size to accelerate the R_x resolution search
    speed_scale = 32
    image_dim = int(min(img.shape[0:2]))

    gray = rgb2gray(img)
    grad = np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)) + np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
    grad = cv2.resize(grad, (image_dim, image_dim), cv2.INTER_AREA)

    # thresholding the gradient map to generate the edge-map as a proxy of the contextual cues
    m = grad.min()
    M = grad.max()
    middle = m + (0.4 * (M - m))
    grad[grad < middle] = 0
    grad[grad >= middle] = 1

    # dilation kernel with size of the receptive field
    kernel = np.ones((int(basesize / speed_scale), int(basesize / speed_scale)), float)
    # dilation kernel with size of the a quarter of receptive field used to compute k
    # as described in section 6 of main paper
    kernel2 = np.ones((int(basesize / (4 * speed_scale)), int(basesize / (4 * speed_scale))), float)

    # Output resolution limit set by the whole_size_threshold and scale_threshold.
    threshold = min(whole_size_threshold, scale_threshold * max(img.shape[:2]))

    outputsize_scale = basesize / speed_scale
    for p_size in range(int(basesize / speed_scale), int(threshold / speed_scale), int(basesize / (2 * speed_scale))):
        grad_resized = resizewithpool(grad, p_size)
        grad_resized = cv2.resize(grad_resized, (p_size, p_size), cv2.INTER_NEAREST)
        grad_resized[grad_resized >= 0.5] = 1
        grad_resized[grad_resized < 0.5] = 0

        dilated = cv2.dilate(grad_resized, kernel, iterations=1)
        meanvalue = (1 - dilated).mean()
        if meanvalue > confidence:
            break
        else:
            outputsize_scale = p_size

    grad_region = cv2.dilate(grad_resized, kernel2, iterations=1)
    patch_scale = grad_region.mean()

    return int(outputsize_scale * speed_scale), patch_scale


# Generate a double-input depth estimation
def doubleestimate(img, size1, size2, pix2pixsize, model, net_type, pix2pixmodel):
    # Generate the low resolution estimation
    estimate1 = singleestimate(img, size1, model, net_type)
    # Resize to the inference size of merge network.
    estimate1 = cv2.resize(estimate1, (pix2pixsize, pix2pixsize), interpolation=cv2.INTER_CUBIC)

    # Generate the high resolution estimation
    estimate2 = singleestimate(img, size2, model, net_type)
    # Resize to the inference size of merge network.
    estimate2 = cv2.resize(estimate2, (pix2pixsize, pix2pixsize), interpolation=cv2.INTER_CUBIC)

    # Inference on the merge model
    pix2pixmodel.set_input(estimate1, estimate2)
    pix2pixmodel.test()
    visuals = pix2pixmodel.get_current_visuals()
    prediction_mapped = visuals['fake_B']
    prediction_mapped = (prediction_mapped + 1) / 2
    prediction_mapped = (prediction_mapped - torch.min(prediction_mapped)) / (
            torch.max(prediction_mapped) - torch.min(prediction_mapped))
    prediction_mapped = prediction_mapped.squeeze().cpu().numpy()

    return prediction_mapped


# Generate a single-input depth estimation
def singleestimate(img, msize, model, net_type):
    if net_type == 0:
        return estimateleres(img, model, msize, msize)
    elif net_type >= 7:
        # np to PIL
        return estimatezoedepth(Image.fromarray(np.uint8(img * 255)).convert('RGB'), model, msize, msize)
    else:
        return estimatemidasBoost(img, model, msize, msize)


def applyGridpatch(blsize, stride, img, box):
    # Extract a simple grid patch.
    counter1 = 0
    patch_bound_list = {}
    for k in range(blsize, img.shape[1] - blsize, stride):
        for j in range(blsize, img.shape[0] - blsize, stride):
            patch_bound_list[str(counter1)] = {}
            patchbounds = [j - blsize, k - blsize, j - blsize + 2 * blsize, k - blsize + 2 * blsize]
            patch_bound = [box[0] + patchbounds[1], box[1] + patchbounds[0], patchbounds[3] - patchbounds[1],
                           patchbounds[2] - patchbounds[0]]
            patch_bound_list[str(counter1)]['rect'] = patch_bound
            patch_bound_list[str(counter1)]['size'] = patch_bound[2]
            counter1 = counter1 + 1
    return patch_bound_list


# Generating local patches to perform the local refinement described in section 6 of the main paper.
def generatepatchs(img, base_size):
    # Compute the gradients as a proxy of the contextual cues.
    img_gray = rgb2gray(img)
    whole_grad = np.abs(cv2.Sobel(img_gray, cv2.CV_64F, 0, 1, ksize=3)) + \
                 np.abs(cv2.Sobel(img_gray, cv2.CV_64F, 1, 0, ksize=3))

    threshold = whole_grad[whole_grad > 0].mean()
    whole_grad[whole_grad < threshold] = 0

    # We use the integral image to speed-up the evaluation of the amount of gradients for each patch.
    gf = whole_grad.sum() / len(whole_grad.reshape(-1))
    grad_integral_image = cv2.integral(whole_grad)

    # Variables are selected such that the initial patch size would be the receptive field size
    # and the stride is set to 1/3 of the receptive field size.
    blsize = int(round(base_size / 2))
    stride = int(round(blsize * 0.75))

    # Get initial Grid
    patch_bound_list = applyGridpatch(blsize, stride, img, [0, 0, 0, 0])

    # Refine initial Grid of patches by discarding the flat (in terms of gradients of the rgb image) ones. Refine
    # each patch size to ensure that there will be enough depth cues for the network to generate a consistent depth map.
    print("Selecting patches ...")
    patch_bound_list = adaptiveselection(grad_integral_image, patch_bound_list, gf)

    # Sort the patch list to make sure the merging operation will be done with the correct order: starting from biggest
    # patch
    patchset = sorted(patch_bound_list.items(), key=lambda x: getitem(x[1], 'size'), reverse=True)
    return patchset


def getGF_fromintegral(integralimage, rect):
    # Computes the gradient density of a given patch from the gradient integral image.
    x1 = rect[1]
    x2 = rect[1] + rect[3]
    y1 = rect[0]
    y2 = rect[0] + rect[2]
    value = integralimage[x2, y2] - integralimage[x1, y2] - integralimage[x2, y1] + integralimage[x1, y1]
    return value


# Adaptively select patches
def adaptiveselection(integral_grad, patch_bound_list, gf):
    patchlist = {}
    count = 0
    height, width = integral_grad.shape

    search_step = int(32 / factor)

    # Go through all patches
    for c in range(len(patch_bound_list)):
        # Get patch
        bbox = patch_bound_list[str(c)]['rect']

        # Compute the amount of gradients present in the patch from the integral image.
        cgf = getGF_fromintegral(integral_grad, bbox) / (bbox[2] * bbox[3])

        # Check if patching is beneficial by comparing the gradient density of the patch to
        # the gradient density of the whole image
        if cgf >= gf:
            bbox_test = bbox.copy()
            patchlist[str(count)] = {}

            # Enlarge each patch until the gradient density of the patch is equal
            # to the whole image gradient density
            while True:

                bbox_test[0] = bbox_test[0] - int(search_step / 2)
                bbox_test[1] = bbox_test[1] - int(search_step / 2)

                bbox_test[2] = bbox_test[2] + search_step
                bbox_test[3] = bbox_test[3] + search_step

                # Check if we are still within the image
                if bbox_test[0] < 0 or bbox_test[1] < 0 or bbox_test[1] + bbox_test[3] >= height \
                        or bbox_test[0] + bbox_test[2] >= width:
                    break

                # Compare gradient density
                cgf = getGF_fromintegral(integral_grad, bbox_test) / (bbox_test[2] * bbox_test[3])
                if cgf < gf:
                    break
                bbox = bbox_test.copy()

            # Add patch to selected patches
            patchlist[str(count)]['rect'] = bbox
            patchlist[str(count)]['size'] = bbox[2]
            count = count + 1

    # Return selected patches
    return patchlist


def impatch(image, rect):
    # Extract the given patch pixels from a given image.
    w1 = rect[0]
    h1 = rect[1]
    w2 = w1 + rect[2]
    h2 = h1 + rect[3]
    image_patch = image[h1:h2, w1:w2]
    return image_patch


class ImageandPatchs:
    def __init__(self, root_dir, name, patchsinfo, rgb_image, scale=1):
        self.root_dir = root_dir
        self.patchsinfo = patchsinfo
        self.name = name
        self.patchs = patchsinfo
        self.scale = scale

        self.rgb_image = cv2.resize(rgb_image, (round(rgb_image.shape[1] * scale), round(rgb_image.shape[0] * scale)),
                                    interpolation=cv2.INTER_CUBIC)

        self.do_have_estimate = False
        self.estimation_updated_image = None
        self.estimation_base_image = None

    def __len__(self):
        return len(self.patchs)

    def set_base_estimate(self, est):
        self.estimation_base_image = est
        if self.estimation_updated_image is not None:
            self.do_have_estimate = True

    def set_updated_estimate(self, est):
        self.estimation_updated_image = est
        if self.estimation_base_image is not None:
            self.do_have_estimate = True

    def __getitem__(self, index):
        patch_id = int(self.patchs[index][0])
        rect = np.array(self.patchs[index][1]['rect'])
        msize = self.patchs[index][1]['size']

        ## applying scale to rect:
        rect = np.round(rect * self.scale)
        rect = rect.astype('int')
        msize = round(msize * self.scale)

        patch_rgb = impatch(self.rgb_image, rect)
        if self.do_have_estimate:
            patch_whole_estimate_base = impatch(self.estimation_base_image, rect)
            patch_whole_estimate_updated = impatch(self.estimation_updated_image, rect)
            return {'patch_rgb': patch_rgb, 'patch_whole_estimate_base': patch_whole_estimate_base,
                    'patch_whole_estimate_updated': patch_whole_estimate_updated, 'rect': rect,
                    'size': msize, 'id': patch_id}
        else:
            return {'patch_rgb': patch_rgb, 'rect': rect, 'size': msize, 'id': patch_id}

    def print_options(self, opt):
        """Print and save options

        It will print both current options and default values(if different).
        It will save options into a text file / [checkpoints_dir] / opt.txt
        """
        message = ''
        message += '----------------- Options ---------------\n'
        for k, v in sorted(vars(opt).items()):
            comment = ''
            default = self.parser.get_default(k)
            if v != default:
                comment = '\t[default: %s]' % str(default)
            message += '{:>25}: {:<30}{}\n'.format(str(k), str(v), comment)
        message += '----------------- End -------------------'
        print(message)

        # save to the disk
        """
        expr_dir = os.path.join(opt.checkpoints_dir, opt.name)
        util.mkdirs(expr_dir)
        file_name = os.path.join(expr_dir, '{}_opt.txt'.format(opt.phase))
        with open(file_name, 'wt') as opt_file:
            opt_file.write(message)
            opt_file.write('\n')
        """

    def parse(self):
        """Parse our options, create checkpoints directory suffix, and set up gpu device."""
        opt = self.gather_options()
        opt.isTrain = self.isTrain  # train or test

        # process opt.suffix
        if opt.suffix:
            suffix = ('_' + opt.suffix.format(**vars(opt))) if opt.suffix != '' else ''
            opt.name = opt.name + suffix

        # self.print_options(opt)

        # set gpu ids
        str_ids = opt.gpu_ids.split(',')
        opt.gpu_ids = []
        for str_id in str_ids:
            id = int(str_id)
            if id >= 0:
                opt.gpu_ids.append(id)
        # if len(opt.gpu_ids) > 0:
        #    torch.cuda.set_device(opt.gpu_ids[0])

        self.opt = opt
        return self.opt


def estimateboost(img, model, model_type, pix2pixmodel):
    # get settings
    if hasattr(opts, 'depthmap_script_boost_rmax'):
        whole_size_threshold = opts.depthmap_script_boost_rmax

    if model_type == 0:  # leres
        net_receptive_field_size = 448
        patch_netsize = 2 * net_receptive_field_size
    elif model_type == 1:  # dpt_beit_large_512
        net_receptive_field_size = 512
        patch_netsize = 2 * net_receptive_field_size
    else:  # other midas
        net_receptive_field_size = 384
        patch_netsize = 2 * net_receptive_field_size

    gc.collect()
    devices.torch_gc()

    # Generate mask used to smoothly blend the local pathc estimations to the base estimate.
    # It is arbitrarily large to avoid artifacts during rescaling for each crop.
    mask_org = generatemask((3000, 3000))
    mask = mask_org.copy()

    # Value x of R_x defined in the section 5 of the main paper.
    r_threshold_value = 0.2
    # if R0:
    #    r_threshold_value = 0

    input_resolution = img.shape
    scale_threshold = 3  # Allows up-scaling with a scale up to 3

    # Find the best input resolution R-x. The resolution search described in section 5-double estimation of the main paper and section B of the
    # supplementary material.
    whole_image_optimal_size, patch_scale = calculateprocessingres(img, net_receptive_field_size, r_threshold_value,
                                                                   scale_threshold, whole_size_threshold)

    print('wholeImage being processed in :', whole_image_optimal_size)

    # Generate the base estimate using the double estimation.
    whole_estimate = doubleestimate(img, net_receptive_field_size, whole_image_optimal_size, pix2pixsize, model,
                                    model_type, pix2pixmodel)

    # Compute the multiplier described in section 6 of the main paper to make sure our initial patch can select
    # small high-density regions of the image.
    global factor
    factor = max(min(1, 4 * patch_scale * whole_image_optimal_size / whole_size_threshold), 0.2)
    print('Adjust factor is:', 1 / factor)

    # Compute the default target resolution.
    if img.shape[0] > img.shape[1]:
        a = 2 * whole_image_optimal_size
        b = round(2 * whole_image_optimal_size * img.shape[1] / img.shape[0])
    else:
        a = round(2 * whole_image_optimal_size * img.shape[0] / img.shape[1])
        b = 2 * whole_image_optimal_size
    b = int(round(b / factor))
    a = int(round(a / factor))

    """
    # recompute a, b and saturate to max res.
    if max(a,b) > max_res:
        print('Default Res is higher than max-res: Reducing final resolution')
        if img.shape[0] > img.shape[1]:
            a = max_res
            b = round(option.max_res * img.shape[1] / img.shape[0])
        else:
            a = round(option.max_res * img.shape[0] / img.shape[1])
            b = max_res
        b = int(b)
        a = int(a)
    """

    img = cv2.resize(img, (b, a), interpolation=cv2.INTER_CUBIC)

    # Extract selected patches for local refinement
    base_size = net_receptive_field_size * 2
    patchset = generatepatchs(img, base_size)

    print('Target resolution: ', img.shape)

    # Computing a scale in case user prompted to generate the results as the same resolution of the input.
    # Notice that our method output resolution is independent of the input resolution and this parameter will only
    # enable a scaling operation during the local patch merge implementation to generate results with the same resolution
    # as the input.
    """
    if output_resolution == 1:
        mergein_scale = input_resolution[0] / img.shape[0]
        print('Dynamicly change merged-in resolution; scale:', mergein_scale)
    else:
        mergein_scale = 1
    """
    # always rescale to input res for now
    mergein_scale = input_resolution[0] / img.shape[0]

    imageandpatchs = ImageandPatchs('', '', patchset, img, mergein_scale)
    whole_estimate_resized = cv2.resize(whole_estimate, (round(img.shape[1] * mergein_scale),
                                                         round(img.shape[0] * mergein_scale)),
                                        interpolation=cv2.INTER_CUBIC)
    imageandpatchs.set_base_estimate(whole_estimate_resized.copy())
    imageandpatchs.set_updated_estimate(whole_estimate_resized.copy())

    print('Resulting depthmap resolution will be :', whole_estimate_resized.shape[:2])
    print('patches to process: ' + str(len(imageandpatchs)))

    # Enumerate through all patches, generate their estimations and refining the base estimate.
    for patch_ind in range(len(imageandpatchs)):

        # Get patch information
        patch = imageandpatchs[patch_ind]  # patch object
        patch_rgb = patch['patch_rgb']  # rgb patch
        patch_whole_estimate_base = patch['patch_whole_estimate_base']  # corresponding patch from base
        rect = patch['rect']  # patch size and location
        patch_id = patch['id']  # patch ID
        org_size = patch_whole_estimate_base.shape  # the original size from the unscaled input
        print('\t processing patch', patch_ind, '/', len(imageandpatchs) - 1, '|', rect)

        # We apply double estimation for patches. The high resolution value is fixed to twice the receptive
        # field size of the network for patches to accelerate the process.
        patch_estimation = doubleestimate(patch_rgb, net_receptive_field_size, patch_netsize, pix2pixsize, model,
                                          model_type, pix2pixmodel)
        patch_estimation = cv2.resize(patch_estimation, (pix2pixsize, pix2pixsize), interpolation=cv2.INTER_CUBIC)
        patch_whole_estimate_base = cv2.resize(patch_whole_estimate_base, (pix2pixsize, pix2pixsize),
                                               interpolation=cv2.INTER_CUBIC)

        # Merging the patch estimation into the base estimate using our merge network:
        # We feed the patch estimation and the same region from the updated base estimate to the merge network
        # to generate the target estimate for the corresponding region.
        pix2pixmodel.set_input(patch_whole_estimate_base, patch_estimation)

        # Run merging network
        pix2pixmodel.test()
        visuals = pix2pixmodel.get_current_visuals()

        prediction_mapped = visuals['fake_B']
        prediction_mapped = (prediction_mapped + 1) / 2
        prediction_mapped = prediction_mapped.squeeze().cpu().numpy()

        mapped = prediction_mapped

        # We use a simple linear polynomial to make sure the result of the merge network would match the values of
        # base estimate
        p_coef = np.polyfit(mapped.reshape(-1), patch_whole_estimate_base.reshape(-1), deg=1)
        merged = np.polyval(p_coef, mapped.reshape(-1)).reshape(mapped.shape)

        merged = cv2.resize(merged, (org_size[1], org_size[0]), interpolation=cv2.INTER_CUBIC)

        # Get patch size and location
        w1 = rect[0]
        h1 = rect[1]
        w2 = w1 + rect[2]
        h2 = h1 + rect[3]

        # To speed up the implementation, we only generate the Gaussian mask once with a sufficiently large size
        # and resize it to our needed size while merging the patches.
        if mask.shape != org_size:
            mask = cv2.resize(mask_org, (org_size[1], org_size[0]), interpolation=cv2.INTER_LINEAR)

        tobemergedto = imageandpatchs.estimation_updated_image

        # Update the whole estimation:
        # We use a simple Gaussian mask to blend the merged patch region with the base estimate to ensure seamless
        # blending at the boundaries of the patch region.
        tobemergedto[h1:h2, w1:w2] = np.multiply(tobemergedto[h1:h2, w1:w2], 1 - mask) + np.multiply(merged, mask)
        imageandpatchs.set_updated_estimate(tobemergedto)

    # output
    return cv2.resize(imageandpatchs.estimation_updated_image, (input_resolution[1], input_resolution[0]),
                      interpolation=cv2.INTER_CUBIC)


def pano_depth_to_world_points(depth):
    """
    360 depth to world points
    given 2D depth is an equirectangular projection of a spherical image
    Treat depth as radius
    longitude : -pi to pi
    latitude : -pi/2 to pi/2
    """

    # Convert depth to radius
    radius = depth.flatten()

    lon = np.linspace(-np.pi, np.pi, depth.shape[1])
    lat = np.linspace(-np.pi / 2, np.pi / 2, depth.shape[0])

    lon, lat = np.meshgrid(lon, lat)
    lon = lon.flatten()
    lat = lat.flatten()

    # Convert to cartesian coordinates
    x = radius * np.cos(lat) * np.cos(lon)
    y = radius * np.cos(lat) * np.sin(lon)
    z = radius * np.sin(lat)

    pts3d = np.stack([x, y, z], axis=1)

    return pts3d


def depth_edges_mask(depth):
    """Returns a mask of edges in the depth map.
    Args:
    depth: 2D numpy array of shape (H, W) with dtype float32.
    Returns:
    mask: 2D numpy array of shape (H, W) with dtype bool.
    """
    # Compute the x and y gradients of the depth map.
    depth_dx, depth_dy = np.gradient(depth)
    # Compute the gradient magnitude.
    depth_grad = np.sqrt(depth_dx ** 2 + depth_dy ** 2)
    # Compute the edge mask.
    mask = depth_grad > 0.05
    return mask


def create_mesh(image, depth, keep_edges=False, spherical=False):
    import trimesh
    maxsize = 1024
    if hasattr(opts, 'depthmap_script_mesh_maxsize'):
        maxsize = opts.depthmap_script_mesh_maxsize

    # limit the size of the input image
    image.thumbnail((maxsize, maxsize))

    if not spherical:
        pts3d = depth_to_points(depth[None])
    else:
        pts3d = pano_depth_to_world_points(depth)

    pts3d = pts3d.reshape(-1, 3)

    verts = pts3d.reshape(-1, 3)
    image = np.array(image)
    if keep_edges:
        triangles = create_triangles(image.shape[0], image.shape[1])
    else:
        triangles = create_triangles(image.shape[0], image.shape[1], mask=~depth_edges_mask(depth))
    colors = image.reshape(-1, 3)

    mesh = trimesh.Trimesh(vertices=verts, faces=triangles, vertex_colors=colors)

    # rotate 90deg over X when spherical
    if spherical:
        angle = math.pi / 2
        direction = [1, 0, 0]
        center = [0, 0, 0]
        rot_matrix = trimesh.transformations.rotation_matrix(angle, direction, center)
        mesh.apply_transform(rot_matrix)

    return mesh


def save_mesh_obj(fn, mesh):
    mesh.export(fn)
