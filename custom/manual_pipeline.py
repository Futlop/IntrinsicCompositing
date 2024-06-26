import os

import numpy as np
from PIL import Image
import skimage.transform

from boosted_depth.depth_util import create_depth_models, get_depth
from chrislib.data_util import load_image
from chrislib.general import invert, uninvert, view, np_to_pil, to2np, add_chan
from chrislib.normal_util import get_omni_normals

import intrinsic.model_util
import intrinsic.pipeline

import intrinsic_compositing.shading.pipeline
from intrinsic_compositing.shading.pipeline import (
    load_reshading_model,
    compute_reshading,
    generate_shd,
    get_light_coeffs
)

import intrinsic_compositing.albedo.pipeline

from omnidata_tools.model_util import load_omni_model

import utils

# ----------------------------------------------------------------- #

def run_full_pipeline(
    bg_im_path: str, 
    fg_im_path: str, # must be transparent; the mask will be extracted
    folder_name: str, 

    fg_top_left_pos,
    fg_scale_relative=0.25, # relative to the size of the output image
    
    max_edge_size=1024
):
    """
    outputs resulting files into `folder_name`
    """

    print("\n1.1 loading depth model")
    depth_model = create_depth_models()

    print("\n1.2 loading intrinsic decomposition model")
    intrinsic_model = intrinsic.model_util.load_models('paper_weights')

    print("\n1.3 loading normals model")
    normals_model = load_omni_model()

    print("\n1.4 loading albedo model")
    albedo_model = intrinsic_compositing.albedo.pipeline.load_albedo_harmonizer()

    print("\n1.5 loading reshading model")
    reshading_model = load_reshading_model('further_trained')

    print("\n2.1 load & resize bg_im")
    bg_im_original = load_image(bg_im_path)
    max_dim = max(bg_im_original.shape[0], bg_im_original.shape[1])
    scale = max_edge_size / max_dim
    bg_height = int(bg_im_original.shape[0] * scale)
    bg_width  = int(bg_im_original.shape[1] * scale)
    bg_im = skimage.transform.resize(bg_im_original, (bg_height, bg_width))
    print(f"\toriginal bg_im shape: {bg_im_original.shape}")
    print(f"\trescaled bg_im shape: {bg_im.shape}")

    print("\n2.2 load, resize, and apply mask to fg_im")
    fg_im_transparent = load_image(fg_im_path)
    fg_im = fg_im_transparent[:, :, 0:3]
    fg_mask = fg_im_transparent[:, :, 3]
    print(f"\tfg_im shape: {fg_im.shape}")
    print(f"\tfg_mask shape: {fg_mask.shape}")

    bb = utils.get_bbox(fg_mask)
    fg_im_crop = fg_im[bb[0]:bb[1], bb[2]:bb[3], :].copy()
    fg_mask_crop = fg_mask[bb[0]:bb[1], bb[2]:bb[3]].copy()
    max_dim = max(fg_im_crop.shape[0], fg_im_crop.shape[1])
    fg_scale = MAX_EDGE_SIZE / max_dim
    cropped_height = int(fg_im_crop.shape[0] * fg_scale)
    cropped_width  = int(fg_im_crop.shape[1] * fg_scale)
    fg_im_crop = skimage.transform.resize(fg_im_crop, (cropped_height, cropped_width))
    fg_mask_crop = skimage.transform.resize(fg_mask_crop, (cropped_height, cropped_width))
    print(f"\tfg_im_crop shape: {fg_im_crop.shape}")
    print(f"\tfg_mask_crop shape: {fg_mask_crop.shape}")
    
    print("\n3.1 generate bg_im depth map") 
    im_depth = get_depth(bg_im, depth_model)

    print("\n3.2 generate fg_im_crop depth map")
    fg_im_depth = get_depth(fg_im_crop, depth_model)

    print("\n4.1 compute bg_im shading & albedo")
    result = intrinsic.pipeline.run_pipeline(
        intrinsic_model,
        bg_im ** 2.2, # TODO: ask why this for gamma correction? https://en.wikipedia.org/wiki/Gamma_correction
        resize_conf=0.0,
        maintain_size=True,
        linear=True,
    )
    im_inv_shading, im_albedo = result["inv_shading"][:, :, np.newaxis], result["albedo"]

    print("\n4.2 compute fg shading & albedo")
    result = intrinsic.pipeline.run_pipeline(
        intrinsic_model,
        fg_im_crop ** 2.2,
        resize_conf=0.0,
        maintain_size=True,
        linear=True,
    )
    fg_im_inv_shading, fg_im_albedo = result["inv_shading"][:, :, np.newaxis], result["albedo"]

    print("\n5.1 compute bg normals")
    im_normals = get_omni_normals(normals_model, bg_im)

    print("\n5.2 compute fg normals")
    # get normals just for the image fragment, it's best to send it through 
    # cropped and scaled to 1024 in order to get the most details, then
    # we resize it to match the fragment size
    fg_im_normals = get_omni_normals(normals_model, fg_im_crop)

    print("\n6. create composited images")

    print("\t6.1 compute rescaled images")
    # rescale computed images for the selected composite location
    fg_scaled_height = int(cropped_height * fg_scale_relative)
    fg_scaled_width  = int(cropped_width  * fg_scale_relative)
    fg_im_rescaled          = skimage.transform.resize(fg_im_crop, (fg_scaled_height, fg_scaled_width), anti_aliasing=True)
    fg_mask_rescaled        = skimage.transform.resize(fg_mask_crop, (fg_scaled_height, fg_scaled_width), anti_aliasing=True)
    fg_depth_rescaled       = skimage.transform.resize(fg_im_depth, (fg_scaled_height, fg_scaled_width), anti_aliasing=True)
    fg_inv_shading_rescaled = skimage.transform.resize(fg_im_inv_shading, (fg_scaled_height, fg_scaled_width), anti_aliasing=True)
    fg_normals_rescaled     = skimage.transform.resize(fg_im_normals, (fg_scaled_height, fg_scaled_width), anti_aliasing=True)

    top  = int(fg_top_left_pos[0] * bg_height)
    left = int(fg_top_left_pos[1] * bg_width)

    print("\t6.2 get fg_rescaled images")
    # composite the mask
    fg_full_mask = np.zeros((bg_height, bg_width), dtype=np.float32)
    fg_full_mask[
        top : top + int(fg_scaled_height), 
        left : left + int(fg_scaled_width),
    ] = fg_mask_rescaled
    fg_full_mask = fg_full_mask[:, :, np.newaxis]
    fg_full_depth = np.zeros((bg_height, bg_width), dtype=np.float32)
    fg_full_depth[
        top : top + int(fg_scaled_height), 
        left : left + int(fg_scaled_width),
    ] = fg_depth_rescaled

    print("\t6.3 get composites")

    comp = utils.composite_crop(
        bg_im,
        (top, left),
        fg_im_rescaled,
        fg_mask_rescaled,
    )
    # NOTE: we're going to control this more in the shadow generation part.
    # TODO: is this required to do any future analyses like harmonization? - we could, but don't have to use the updated one
    comp_depth = utils.composite_depth(
        im_depth,
        (top, left),
        fg_depth_rescaled,
        fg_mask_rescaled,
    )
    comp_inv_shading = utils.composite_crop(
        im_inv_shading,
        (top, left),
        fg_inv_shading_rescaled,
        fg_mask_rescaled,
    )
    comp_normals = utils.composite_crop(
        im_normals, 
        (top, left),
        fg_normals_rescaled,
        fg_mask_rescaled,
    )

    print("\n7.1 harmonize albedo")

    # the albedo comes out gamma corrected so make it linear
    comp_albedo_harmonized = intrinsic_compositing.albedo.pipeline.harmonize_albedo(
        comp,
        fg_full_mask,
        comp_inv_shading, 
        albedo_model,
        reproduce_paper=False,
    ) ** 2.2

    original_albedo = (comp ** 2.2) / uninvert(comp_inv_shading)
    # Q: why does this result look so weird? it just does
    comp_harmonized = comp_albedo_harmonized * uninvert(comp_inv_shading)

    print("\n7.2 get shading coefficients")

    # to ensure that normals are globally accurate we compute them at
    # a resolution of 512 pixels, so resize our shading and image to compute 
    # rescaled normals, then run the lighting model optimization
    max_dim = max(bg_height, bg_width)
    small_height = int(bg_height * (512.0 / max_dim))
    small_width = int(bg_width * (512.0 / max_dim))
    small_bg_im = skimage.transform.resize(bg_im, (small_height, small_width), anti_aliasing=True)
    small_bg_normals = get_omni_normals(normals_model, small_bg_im)
    small_bg_inv_shading = skimage.transform.resize(im_inv_shading, (small_height, small_width), anti_aliasing=True)

    # TODO: what is light_vis?
    coeffs, light_vis = intrinsic_compositing.shading.pipeline.get_light_coeffs(
        small_bg_inv_shading[:, :, 0], 
        small_bg_normals,
        small_bg_im,
    )

    print("[x, y, z], c")
    print(coeffs)

    print("\n7.3 run reshading model")

    # run the reshading model using the various composited components,
    # and our lighting coefficients from the user interface
    main_result = compute_reshading(
        comp_harmonized,
        fg_full_mask,
        comp_inv_shading,
        comp_depth,
        comp_normals,
        comp_albedo_harmonized,
        coeffs,
        reshading_model,
    )

    print("\n8. write images")
    
    bg_im_name = os.path.basename(bg_im_path).split(".")[0]
    fg_im_name = os.path.basename(fg_im_path).split(".")[0]

    os.makedirs(f"output/{folder_name}/", exist_ok=True)

    np_to_pil(bg_im).save(f"output/{folder_name}/{bg_im_name}.png")
    np_to_pil(im_depth).save(f"output/{folder_name}/{bg_im_name}_depth.png")
    np_to_pil(im_inv_shading[:, :, 0]).save(f"output/{folder_name}/{bg_im_name}_inv_shading.png")
    np_to_pil(im_albedo).save(f"output/{folder_name}/{bg_im_name}_albedo.png")
    np_to_pil(im_normals).save(f"output/{folder_name}/{bg_im_name}_normals.png")

    np_to_pil(fg_full_mask[:, :, 0]).save(f"output/{folder_name}/{fg_im_name}_full_mask.png")
    np_to_pil(fg_full_depth).save(f"output/{folder_name}/{fg_im_name}_full_depth.png")

    # NOTE: despite the naming convention, all images are the "cropped" versions
    # np_to_pil(fg_im_crop).save(f"output/{folder_name}/{fg_im_name}.png")
    # np_to_pil(fg_mask_crop).save(f"output/{folder_name}/{fg_im_name}_mask.png")
    # np_to_pil(fg_im_depth).save(f"output/{folder_name}/{fg_im_name}_depth.png")
    # np_to_pil(fg_im_inv_shading).save(f"output/{folder_name}/{fg_im_name}_inv_shading.png")
    np_to_pil(fg_im_albedo).save(f"output/{folder_name}/{fg_im_name}_albedo.png")
    # np_to_pil(fg_im_normals).save(f"output/{folder_name}/{fg_im_name}_normals.png")

    np_to_pil(comp).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}.png")
    #np_to_pil(comp_mask).save(f"output/{folder_name}/{fg_im_name}_mask.png")
    np_to_pil(comp_depth).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_depth.png")
    #np_to_pil(comp_inv_shading).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_inv_shading.png")
    #np_to_pil(comp_albedo).save(f"output/{folder_name}/{fg_im_name}_albedo.png")
    np_to_pil(comp_normals).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_normals.png")
    np_to_pil(comp_harmonized).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_harmonized.png")
    np_to_pil(comp_albedo_harmonized).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_albedo_harmonized.png")
    np_to_pil(original_albedo).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_original_albedo.png")

    np_to_pil(main_result['composite']).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_main_result.png")
    #np_to_pil(main_result['reshading']).save(f"output/{folder_name}/{bg_im_name}_{fg_im_name}_main_result_2.png")

# ----------------------------------------------- #
# config 

#BG_IM_PATH = "../../background/map-8526430.jpg"
BG_IM_PATH = "../../background/door-8453898.jpg"
#BG_IM_PATH = "../../background/trees-8512979.jpg"
#BG_IM_PATH = "../../background/sheet-music-8463988.jpg"
#BG_IM_PATH = "../../background/soap-8429699.jpg" # TODO: test removing gamma correction for the soap example
#BG_IM_PATH = "../../background/IMG_1520.jpg"
#BG_IM_PATH = "../../background/cycling-8215973.jpg"

#FG_IM_PATH = "../../foreground/dressing-table-947429.png"
#FG_IM_PATH = "../../foreground/trolley-2582492.png"
#FG_IM_PATH = "../../foreground/shampoo-1860642.png"
FG_IM_PATH = "../../foreground/lotus-3192656.png"

FOLDER_NAME = "lotus-door"

MAX_EDGE_SIZE = 1024
FG_RELATIVE_SCALE = 0.25 # how large the fg image should be when compared to the bg
FG_TOP_LEFT_POS = [0.35, 0.34]

# ----------------------------------------------- #
# main

if __name__ == "__main__":

    run_full_pipeline(
        BG_IM_PATH,
        FG_IM_PATH,
        FOLDER_NAME, 

        FG_TOP_LEFT_POS,
        FG_RELATIVE_SCALE, # relative to the size of the output image

        MAX_EDGE_SIZE
    )