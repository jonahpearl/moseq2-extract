'''
Video pre-processing utilities for detecting ROIs and extracting raw data.
'''

import cv2
import math
import joblib
import scipy.stats
import numpy as np
import matplotlib.pyplot as plt
import pickle
import pdb
import scipy.signal
import scipy.interpolate
import skimage.measure
import skimage.morphology
from skimage import color
from skimage.transform import hough_ellipse
from skimage.feature import canny
from skimage.draw import circle_perimeter, ellipse_perimeter, ellipse
from skimage.util import img_as_ubyte
from copy import deepcopy
from tqdm.auto import tqdm
import moseq2_extract.io.video
import moseq2_extract.extract.roi
from os.path import exists, join, dirname
from os import makedirs
from moseq2_extract.io.image import read_image, write_image
from moseq2_extract.util import convert_pxs_to_mm, strided_app


def get_flips(frames, flip_file=None, smoothing=None):
    '''
    Predicts frames where mouse orientation is flipped to later correct.
    If the given flip file is not found or valid, a warning will be emitted and the
    video will not be flipped.

    Parameters
    ----------
    frames (3d numpy array): frames x r x c, cropped mouse
    flip_file (str): path to joblib dump of scipy random forest classifier
    smoothing (int): kernel size for median filter smoothing of random forest probabilities

    Returns
    -------
    flips (bool array):  true for flips
    '''

    try:
        clf = joblib.load(flip_file)
    except IOError:
        print(f"Could not open file {flip_file}")
        raise

    flip_class = np.where(clf.classes_ == 1)[0]

    try:
        probas = clf.predict_proba(
            frames.reshape((-1, frames.shape[1] * frames.shape[2])))
    except ValueError:
        print('WARNING: Input crop-size is not compatible with flip classifier.')
        accepted_crop = int(math.sqrt(clf.n_features_))
        print(f'Adjust the crop-size to ({accepted_crop}, {accepted_crop}) to use this flip classifier.')
        print('The extracted data will NOT be flipped!')
        probas = np.array([[0]*len(frames), [1]*len(frames)]).T # default output; indicating no flips

    if smoothing:
        for i in range(probas.shape[1]):
            probas[:, i] = scipy.signal.medfilt(probas[:, i], smoothing)

    flips = probas.argmax(axis=1) == flip_class

    return flips


def get_largest_cc(frames, progress_bar=False):
    '''
    Returns largest connected component blob in image

    Parameters
    ----------
    frames (3d numpy array): frames x r x c, uncropped mouse
    progress_bar (bool): display progress bar

    Returns
    -------
    flips (3d bool array):  frames x r x c, true where blob was found
    '''

    foreground_obj = np.zeros((frames.shape), 'bool')

    for i in tqdm(range(frames.shape[0]), disable=not progress_bar, desc='CC'):
        nb_components, output, stats, centroids =\
            cv2.connectedComponentsWithStats(frames[i], connectivity=4)
        szs = stats[:, -1]
        foreground_obj[i] = output == szs[1:].argmax()+1

    return foreground_obj


def get_bground_im_file(frames_file, frame_stride=500, med_scale=5, output_dir=None, **kwargs):
    '''
    Returns background from file. If the file is not found, session frames will be read in
     and a median frame (background) will be computed.

    Parameters
    ----------
    frames_file (str): path to data with frames
    frame_stride (int): stride size between frames for median bground calculation
    med_scale (int): kernel size for median blur for background images.
    kwargs (dict): extra keyword arguments

    Returns
    -------
    bground (2d numpy array):  r x c, background image
    '''

    if output_dir is None:
        bground_path = join(dirname(frames_file), 'proc', 'bground.tiff')
    else:
        bground_path = join(output_dir, 'bground.tiff')

    kwargs = deepcopy(kwargs)
    finfo = kwargs.pop('finfo', None)

    # Compute background image if it doesn't exist. Otherwise, load from file
    if not exists(bground_path) or kwargs.get('recompute_bg', False):
        if finfo is None:
            finfo = moseq2_extract.io.video.get_movie_info(frames_file, **kwargs)

        frame_idx = np.arange(0, finfo['nframes'], frame_stride)
        frame_store = []
        for i, frame in enumerate(frame_idx):
            frs = moseq2_extract.io.video.load_movie_data(frames_file,
                                                          [int(frame)], 
                                                          frame_size=finfo['dims'], 
                                                          finfo=finfo, 
                                                          **kwargs).squeeze()
            frame_store.append(cv2.medianBlur(frs, med_scale))

        bground = np.nanmedian(frame_store, axis=0)
        
        # JP edit
        if kwargs.get('remove_obj_from_bg', False):
            
            print('Removing object from background via RANSAC plane interpolation... (takes a few mins)')
            
            # Get any relevant params passed, otherwise set defaults (are also set within the fns)
            floor_pctile = kwargs.pop('floor_pctile', 99)
            floor_range = kwargs.pop('floor_range', 50) # in mm
            erosion_size = kwargs.pop('erosion_size', 6) # size of strel disk in pixels
            
            # Set path to save intermediate output images
            if output_dir is None:
                obj_removal_path = join(dirname(frames_file), 'proc', 'obj_removal')
            else:
                obj_removal_path = join(output_dir, 'obj_removal')
 
            try:
                makedirs(obj_removal_path, exist_ok=False)
            except FileExistsError:
                pass
  
            # Run the algorithm to interpolate the floor (RANSAC plane) underneath the object
            interp_bkgd, best_plane, ellipse_params, floor_roi, box_roi, mean_box_height = interp_elliptical_floor(bground, erosion_size=erosion_size, save_dir=obj_removal_path)
            bground = interp_bkgd

            # Save ROI info
            with open(join(obj_removal_path, 'obj_removal_info.p'), 'wb') as f:
                pickle.dump((floor_roi, box_roi, mean_box_height), f)

        # Save background if we just calculated it
        write_image(bground_path, bground, scale=True)

    else: # if background image already existed
        print('Background already exists, reloading...')
        bground = read_image(bground_path, scale=True)
        
    return bground

                  
# JP Functions for object removal from bg

# Main function
def interp_elliptical_floor(bkgd, floor_pctile=99, floor_range=50, save_dir='.', erosion_size=6):
    """
    Use RANSAC plane fitting to interpolate the floor underneath an object in the MOSEQ background.
    
    Inputs:
        bkgd (np.array): median-filtered background image
        floor_pctile (int): parameter used to distinguish the floor from the object. Usually close to 100.
        floor_range (int): another paramter used to distinguish the floor. 
                     The depth of the object should be no higher than (np.percentile(bkgd[bkgd!=0], percentile) - floor_range).
                     Ie if the floor is ~ 500 - 520, and the object is 480, you might set range to 25 or so.
        save_dir (string): where to save partial results plots
        erosion_size (int): size of strel disk used at end to clean up the fit ROIs. Could be further parameterized into two different sizes. 
    
    Returns:
        interp_bkgd (np.array): the new background image
        best_plane (np.array): 4 parameters a,b,c,d for the best-fit plane
        (yc, xc, a, b, orientation): tuple of parameters for skimage's ellipse
        f: floor mask
        box_roi: box mask
        mean_box_height: average depth of bkgd pixels within box mask (for later use to find mouse above box)
    """
    
    # Get outline of floor without the object
    f = get_rough_floor(bkgd, percentile=floor_pctile, rng=floor_range)
    
    # Fit an ellipse (aiming for the true outline of the bucket)
    (yc, xc, a, b, orientation) = get_floor_ellipse(f) # takes a few minutes
    
    # Save partial results for manual inspection
    plot_fitted_floor_ellipse(f, yc, xc, a, b, orientation, save_dir=save_dir)
    
    # Interpolate plane onto the part of the bucket floor that we can see
    masked_bkgd = deepcopy(bkgd)
    masked_bkgd[~f] = 0 # depth values for the bucket floor that we can see
    min_floor = 100*(np.floor(np.min(masked_bkgd[masked_bkgd!=0])/100))
    max_floor = 100*(np.ceil(np.max(masked_bkgd)/100))
    best_plane, dist = moseq2_extract.extract.roi.plane_ransac(masked_bkgd, 
             bg_roi_depth_range=(min_floor, max_floor), iters=1000,
             noise_tolerance=30, in_ratio=0.1,
             progress_bar=True, mask=None)
    
    # Evaluate the interpolated plane across the entire image
    ymax,xmax = f.shape
    yy,xx = np.meshgrid(np.arange(0,ymax,1), np.arange(0,xmax,1))
    yx = np.array([yy.flatten(), xx.flatten()]).T # N x 2 list of points (yi,xi): (y1,x1), (y2,x1), (y3,x1),...
    zvals = eval_plane_z(best_plane, yx).reshape((ymax,xmax), order = 'F') # Fortran-order to fill columns ("y") first

    
    # Save partial results for manual inspection
    plot_fitted_floor_plane(zvals, masked_bkgd, save_dir=save_dir)
    
    # Slot the zvals into the floor ROI
    fdil = skimage.morphology.binary_erosion(f, skimage.morphology.disk(erosion_size)) # erode the floor ROI a bit, to help with the box's shadow
    xx, yy = ellipse(yc, xc, a, b, rotation=orientation)  # points within the fit ellipse
    ellipse_mask = np.zeros(f.shape)
    ellipse_mask[xx, yy] = 1
    ellipse_mask = (ellipse_mask == 1)
    ellipse_mask = skimage.morphology.binary_erosion(ellipse_mask, skimage.morphology.disk(erosion_size)) # erode ellipse ROI a bit to keep the bucket's walls intact
    interp_bkgd = deepcopy(masked_bkgd)
    interp_bkgd[ellipse_mask & ~fdil] = zvals[ellipse_mask &  ~fdil]

    # Make box roi the long way (shorter way gave wrong shape?)
    box_roi = np.zeros(f.shape)
    box_roi[ellipse_mask & ~fdil] = 1
    box_roi = (box_roi == 1)
    box_roi = get_largest_cc((box_roi[np.newaxis,:]).astype('uint8')).squeeze() # requires 3D, uint8 input
    mean_box_height = np.abs(np.mean(bkgd[box_roi]) - np.mean(zvals[ellipse_mask &  ~fdil]))

    # Save results for manual inspection
    plt.figure()
    plt.imshow(interp_bkgd, vmin = np.min(interp_bkgd[interp_bkgd!=0]), vmax = np.max(interp_bkgd))
    plt.colorbar()
    plt.title('Interpolated background')
    plt.savefig(join(save_dir, 'interp_bkgd.tiff'))
    
    return interp_bkgd, best_plane, (yc, xc, a, b, orientation), f, box_roi, mean_box_height
                  
def eval_plane_z(params, yx):
    """
    Evaluates a plane ax+by+cz+d=0 at points (x,y)
    Inputs:
        params (np.array): a,b,c,d in ax+by+cz+d=0
        yx (np.array): Nx2 points to evaluate. Each row is (y,x).
    
    So, z = (-1/c)(ax+by+d)
    """
    first_term = -1/params[2]
    points = params[0]*yx[:,1] + params[1]*yx[:,0] + params[3] # this is the magic line, along with reshape(order='F')
    return first_term*points

def get_rough_floor(bkgd, percentile, rng):
    aFloorVal = np.percentile(bkgd[bkgd!=0], percentile)
#     roughFloorMask = np.logical_and((bkgd < (aFloorVal+rng)), bkgd > (aFloorVal-rng))
    roughFloorMask = bkgd > (aFloorVal-rng)
    return roughFloorMask

def get_floor_ellipse(roughFloorMask, min_size=50, e_threshold=0.45):
    """
    Uses skimage.transform.hough_ellipse() to fit an ellipse to the inferred bucket floor.
    The fit can be a bit slow.
    Inputs: 
        roughFloorMask (np.array): binary mask of floor
        min_size (int): minimum size of ellipse major axis.
        e_threshold: minimum eccentricity to be considered a good fit. Chosen by manual inspection.
    Returns:
        xc: x-coord of center
        yc: y-coord of center
        a: major axis len
        b: minor axis len
        orientation: orientation for the ellipse's major axis
    """
    
    # Get algorithm input
    image = img_as_ubyte(roughFloorMask)
    edges = canny(image, sigma=3, low_threshold=10, high_threshold=50)
    
    # Run skimage algorithm
    result = hough_ellipse(edges, accuracy=20, threshold=250,
                           min_size=min_size)
    result.sort(order='accumulator') # List of estimated parameters for each fit ellipse
    
    # Re-run if result is empty, bc threshold is too low
    thresh = 250
    while len(result) == 0:
        thresh -= 50
        result = hough_ellipse(edges, accuracy=20, threshold=thresh,
                           min_size=min_size)
        result.sort(order='accumulator') # List of estimated para
    
    
    # Sometimes with lower thresholds, the best fit is wrong.
    # Luckily these bad fits tend to be more eccentric than the right fit, so we can filter.
    
    # First, look at the best fit, and if it's the right eccentricity, take it.
    # Otherwise, go backwards through best fits until we find one that works.
    best = list(result[-1])
    yc, xc, a, b = [int(round(x)) for x in best[1:5]]
    orientation = best[5]
    ratio_best = (b**2)/(a**2) # sometimes a and b are flipped, so check for that.
    if ratio_best > 1:
        ratio_best = ratio_best**-1
    ecc_current = np.sqrt(1 - ratio_best)
    ii = -1
    while ecc_current > e_threshold:
        ii -= 1
        next_params = list(result[ii])
        yc, xc, a, b = [int(round(x)) for x in next_params[1:5]]
        orientation = next_params[5]
        ratio_best = (b**2)/(a**2) # sometimes a and b are flipped, so check for that.
        if ratio_best > 1:
            ratio_best = ratio_best**-1
        ecc_current = np.sqrt(1 - ratio_best)
    
    return (yc,xc,a,b,orientation)
    

def plot_fitted_floor_ellipse(roughFloorMask, yc, xc, a, b, orientation, save_dir):
    image = img_as_ubyte(roughFloorMask)
    edges = canny(image, sigma=3, low_threshold=10, high_threshold=50)

    # Draw the ellipse on the original image
    cy, cx = ellipse_perimeter(yc, xc, a, b, orientation)
    image[cy, cx] = 150

    # Draw the edge (white) and the resulting ellipse (red)
    edges = color.gray2rgb(img_as_ubyte(edges))
    edges[cy, cx] = (250, 0, 0)

    fig2, (ax1, ax2) = plt.subplots(ncols=2, nrows=1, figsize=(8, 4),
                                    sharex=True, sharey=True)
    ax1.set_title('Original picture')
    ax1.imshow(image)
    ax2.set_title('Edge (white) and result (red)')
    ax2.imshow(edges)
    plt.savefig(join(save_dir, 'elliptical_background.tiff'))
    return


def plot_fitted_floor_plane(zvals, masked_bkgd, save_dir):
    # Do some visual verifications
    min_val = np.min([np.min(zvals), np.min(masked_bkgd[masked_bkgd!=0])])
    max_val = np.max([np.max(zvals), np.max(masked_bkgd)])
    plt.figure()
    plt.subplot(1,2,1)
    plt.imshow(zvals, vmin = min_val, vmax = max_val, cmap = 'jet')
    plt.colorbar()
    plt.title('Fit plane')
    plt.subplot(1,2,2)
    plt.imshow(masked_bkgd, vmin = min_val, vmax = max_val, cmap = 'jet')
    plt.title('Masked background')
    plt.savefig(join(save_dir, 'fit_floor_plane.tiff'))
    return


# End object-removal functions
                  

def get_bbox(roi):
    '''
    Given a binary mask, return an array with the x and y boundaries

    Parameters
    ----------
    roi (2d np.ndarray): ROI boolean mask to calculate bounding box.

    Returns
    -------
    bbox (2d np.ndarray): Bounding Box around ROI
    '''

    y, x = np.where(roi > 0)

    if len(y) == 0 or len(x) == 0:
        return None
    else:
        bbox = np.array([[y.min(), x.min()], [y.max(), x.max()]])
        return bbox

def threshold_chunk(chunk, min_height, max_height):
    '''
    Thresholds out depth values that are less than min_height and larger than
    max_height.

    Parameters
    ----------
    chunk (3D np.ndarray): Chunk of frames to threshold (nframes, width, height)
    min_height (int): Minimum depth values to include after thresholding.
    max_height (int): Maximum depth values to include after thresholding.
    dilate_iterations (int): Number of iterations the ROI was dilated.

    Returns
    -------
    chunk (3D np.ndarray): Updated frame chunk.
    '''

    chunk[chunk < min_height] = 0
    chunk[chunk > max_height] = 0

    return chunk

def get_roi(depth_image,
            strel_dilate=cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)),
            dilate_iterations=0,
            erode_iterations=0,
            strel_erode=None,
            noise_tolerance=30,
            bg_roi_weights=(1, .1, 1),
            overlap_roi=None,
            bg_roi_gradient_filter=False,
            bg_roi_gradient_kernel=7,
            bg_roi_gradient_threshold=3000,
            bg_roi_fill_holes=True,
            get_all_data=False,
            **kwargs):
    '''
    Compute an ROI using RANSAC plane fitting and simple blob features.

    Parameters
    ----------
    depth_image (2d np.ndarray): Singular depth image frame.
    strel_dilate (cv2.StructuringElement - Rectangle): dilation shape to use.
    dilate_iterations (int): number of dilation iterations.
    erode_iterations (int): number of erosion iterations.
    strel_erode (int): image erosion kernel size.
    noise_tolerance (int): threshold to use for noise filtering.
    bg_roi_weights (tuple): weights describing threshold to accept ROI.
    overlap_roi (np.ndarray): list of ROI boolean arrays to possibly combine.
    bg_roi_gradient_filter (bool): Boolean for whether to use a gradient filter.
    bg_roi_gradient_kernel (tuple): Kernel size of length 2, e.g. (1, 1.5)
    bg_roi_gradient_threshold (int): Threshold for noise gradient filtering
    bg_roi_fill_holes (bool): Boolean to fill any missing regions within the ROI.
    get_all_data (bool): If True, returns all ROI data, else, only return ROIs and computed Planes
    kwargs (dict) Dictionary containing `bg_roi_depth_range` parameter for plane_ransac()

    Returns
    -------
    rois (list): list of 2d roi images.
    roi_plane (2d np.ndarray): computed ROI Plane using RANSAC.
    bboxes (list): list of computed bounding boxes for each respective ROI.
    label_im (list): list of scikit-image image properties
    ranks (list): list of ROI ranks.
    shape_index (list): list of rank means.
    '''

    if bg_roi_gradient_filter:
        gradient_x = np.abs(cv2.Sobel(depth_image, cv2.CV_64F,
                                      1, 0, ksize=bg_roi_gradient_kernel))
        gradient_y = np.abs(cv2.Sobel(depth_image, cv2.CV_64F,
                                      0, 1, ksize=bg_roi_gradient_kernel))
        mask = np.logical_and(gradient_x < bg_roi_gradient_threshold, gradient_y < bg_roi_gradient_threshold)
    else:
        mask = None

    roi_plane, dists = moseq2_extract.extract.roi.plane_ransac(
        depth_image, noise_tolerance=noise_tolerance, mask=mask, **kwargs)
    dist_ims = dists.reshape(depth_image.shape)

    if bg_roi_gradient_filter:
        dist_ims[~mask] = np.inf

    bin_im = dist_ims < noise_tolerance

    # anything < noise_tolerance from the plane is part of it
    label_im = skimage.measure.label(bin_im)
    region_properties = skimage.measure.regionprops(label_im)

    areas = np.zeros((len(region_properties),))
    extents = np.zeros_like(areas)
    dists = np.zeros_like(extents)

    # get the max distance from the center, area and extent
    center = np.array(depth_image.shape)/2

    for i, props in enumerate(region_properties):
        areas[i] = props.area
        extents[i] = props.extent
        tmp_dists = np.sqrt(np.sum(np.square(props.coords-center), 1))
        dists[i] = tmp_dists.max()

    # rank features
    ranks = np.vstack((scipy.stats.rankdata(-areas, method='max'),
                       scipy.stats.rankdata(-extents, method='max'),
                       scipy.stats.rankdata(dists, method='max')))
    weight_array = np.array(bg_roi_weights, 'float32')
    shape_index = np.mean(np.multiply(ranks.astype('float32'), weight_array[:, np.newaxis]), 0).argsort()

    # expansion microscopy on the roi
    rois = []
    bboxes = []

    # Perform image processing on each found ROI
    for shape in shape_index:
        roi = np.zeros_like(depth_image)
        roi[region_properties[shape].coords[:, 0],
            region_properties[shape].coords[:, 1]] = 1
        if strel_dilate is not None:
            roi = cv2.dilate(roi, strel_dilate, iterations=dilate_iterations) # Dilate
        if strel_erode is not None:
            roi = cv2.erode(roi, strel_erode, iterations=erode_iterations) # Erode
        if bg_roi_fill_holes:
            roi = scipy.ndimage.morphology.binary_fill_holes(roi) # Fill Holes

        rois.append(roi)
        bboxes.append(get_bbox(roi))

    # Remove largest overlapping found ROI
    if overlap_roi is not None:
        overlaps = np.zeros_like(areas)

        for i in range(len(rois)):
            overlaps[i] = np.sum(np.logical_and(overlap_roi, rois[i]))

        del_roi = np.argmax(overlaps)
        del rois[del_roi]
        del bboxes[del_roi]

    if get_all_data == True:
        return rois, roi_plane, bboxes, label_im, ranks, shape_index
    else:
        return rois, roi_plane


def apply_roi(frames, roi):
    '''
    Apply ROI to data, consider adding constraints (e.g. mod32==0).

    Parameters
    ----------
    frames (3d np.ndarray): input frames to apply ROI.
    roi (2d np.ndarray): selected ROI to extract from input images.

    Returns
    -------
    cropped_frames (3d np.ndarray): Frames cropped around ROI Bounding Box.
    '''

    # yeah so fancy indexing slows us down by 3-5x
    cropped_frames = frames*roi
    bbox = get_bbox(roi)
    cropped_frames = cropped_frames[:, bbox[0, 0]:bbox[1, 0], bbox[0, 1]:bbox[1, 1]]
    return cropped_frames

def apply_roi_to_mask(mask, roi):
    """
    Same as apply_roi(), but takes a 2D mask and shrinks it
    """
    bbox = get_bbox(roi)
    cropped_mask = mask[bbox[0, 0]:bbox[1, 0], bbox[0, 1]:bbox[1, 1]]
    return cropped_mask


def im_moment_features(IM):
    '''
    Use the method of moments and centralized moments to get image properties.

    Parameters
    ----------
    IM (2d numpy array): depth image

    Returns
    -------
    features (dict): returns a dictionary with orientation,
        centroid, and ellipse axis length
    '''

    tmp = cv2.moments(IM)
    num = 2*tmp['mu11']
    den = tmp['mu20']-tmp['mu02']

    common = np.sqrt(4*np.square(tmp['mu11'])+np.square(den))

    if tmp['m00'] == 0:
        features = {
            'orientation': np.nan,
            'centroid': np.nan,
            'axis_length': [np.nan, np.nan]}
    else:
        features = {
            'orientation': -.5*np.arctan2(num, den),
            'centroid': [tmp['m10']/tmp['m00'], tmp['m01']/tmp['m00']],
            'axis_length': [2*np.sqrt(2)*np.sqrt((tmp['mu20']+tmp['mu02']+common)/tmp['m00']),
                            2*np.sqrt(2)*np.sqrt((tmp['mu20']+tmp['mu02']-common)/tmp['m00'])]
        }

    return features


def clean_frames(frames, prefilter_space=(3,), prefilter_time=None,
                 strel_tail=cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)),
                 iters_tail=None, frame_dtype='uint8',
                 strel_min=cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
                 iters_min=None, progress_bar=False):
    '''
    Simple temporal and/or spatial filtering, median filter and morphological opening.

    Parameters
    ----------
    frames (3d np.ndarray): Frames (nframes x r x c) to filter.
    prefilter_space (tuple): kernel size for spatial filtering
    prefilter_time (tuple): kernel size for temporal filtering
    strel_tail (cv2.StructuringElement): Element for tail filtering.
    iters_tail (int): number of iterations to run opening
    frame_dtype (str): frame encodings
    strel_min (int): minimum kernel size
    iters_min (int): minimum number of filtering iterations
    progress_bar (bool): display progress bar

    Returns
    -------
    filtered_frames (3d np array): frame x r x c
    '''

    # seeing enormous speed gains w/ opencv
    filtered_frames = frames.copy().astype(frame_dtype)

    for i in tqdm(range(frames.shape[0]), disable=not progress_bar, desc='Cleaning frames'):
        # Erode Frames
        if iters_min is not None and iters_min > 0:
            filtered_frames[i] = cv2.erode(filtered_frames[i], strel_min, iters_min)
        # Median Blur
        if prefilter_space is not None and np.all(np.array(prefilter_space) > 0):
            for j in range(len(prefilter_space)):
                filtered_frames[i] = cv2.medianBlur(filtered_frames[i], prefilter_space[j])
        # Tail Filter
        if iters_tail is not None and iters_tail > 0:
            filtered_frames[i] = cv2.morphologyEx(filtered_frames[i], cv2.MORPH_OPEN, strel_tail, iters_tail)

    # Temporal Median Filter
    if prefilter_time is not None and np.all(np.array(prefilter_time) > 0):
        for j in range(len(prefilter_time)):
            filtered_frames = scipy.signal.medfilt(filtered_frames, [prefilter_time[j], 1, 1])

    return filtered_frames


def get_frame_features(frames, frame_threshold=10, mask=np.array([]),
                       mask_threshold=-30, use_cc=False, progress_bar=False,
                       roi=None, **kwargs):
    '''
    Use image moments to compute features of the largest object in the frame

    Parameters
    ----------
    frames (3d np.ndarray): input frames
    frame_threshold (int): threshold in mm separating floor from mouse
    mask (3d np.ndarray): input frame mask for parts not to filter.
    mask_threshold (int): threshold to include regions into mask.
    use_cc (bool): Use connected components.
    progress_bar (bool): Display progress bar.
    roi: the roi for the raw data, to crop other masks if required
    Returns
    -------
    features (dict of lists): dictionary with simple image features
    mask (3d np.ndarray): input frame mask.
    '''

    nframes = frames.shape[0]

    # Get frame mask
    if type(mask) is np.ndarray and mask.size > 0:
        has_mask = True
    else:
        has_mask = False
        mask = np.zeros((frames.shape), 'uint8')

    # Pack contour features into dict
    features = {
        'centroid': np.full((nframes, 2), np.nan),
        'orientation': np.full((nframes,), np.nan),
        'axis_length': np.full((nframes, 2), np.nan)
    }

    for i in tqdm(range(nframes), disable=not progress_bar, desc='Computing moments'):
        
        
        # Threshold frame to compute mask
        if kwargs.get('remove_obj_from_bg', False): 
            
            # Object removal special case: use different heights for floor / object
            # Assumes object has a flat surface of depth mean_box_height
            if i == 1:
                print('Using special case for frame thresholding (obj removal)')
            
            # Get floor / box masks
            obj_removal_path = join(kwargs.get('output_dir'), 'obj_removal')
            with open(join(obj_removal_path, 'obj_removal_info.p'), 'rb') as f:
                floor_mask, box_mask, mean_box_height = pickle.load(f)
            
            # Crop the masks to match the cropped frame
            floor_mask = apply_roi_to_mask(floor_mask, roi)
            box_mask = apply_roi_to_mask(box_mask, roi)

            # Get frame mask 
            floor_threshold = frame_threshold
            box_threshold = frame_threshold + abs(mean_box_height) + 5
            floor_accept = (frames[i] > floor_threshold) & (floor_mask)
            box_accept = (frames[i] > box_threshold) & (box_mask)
            frame_mask = (floor_accept | box_accept)

        else: 
            # Typical case
            frame_mask = frames[i] > frame_threshold

        # Incorporate largest connected component with frame mask
        if use_cc and not (kwargs.get('remove_obj_from_bg', False)):
            cc_mask = get_largest_cc((frames[[i]] > mask_threshold).astype('uint8')).squeeze()
            frame_mask = np.logical_and(cc_mask, frame_mask)
        elif use_cc and (kwargs.get('remove_obj_from_bg', False)):
            floor_threshold = mask_threshold
            box_threshold = mask_threshold + mean_box_height
            floor_accept = (frames[i] > floor_threshold) & (floor_mask)
            box_accept = (frames[i] > box_threshold) & (box_mask)
            tmp = (floor_accept | box_accept)
            cc_mask = get_largest_cc((tmp[np.newaxis,:]).astype('uint8')).squeeze()
            frame_mask = np.logical_and(cc_mask, frame_mask)

        # Apply mask
        if has_mask:
            frame_mask = np.logical_and(frame_mask, mask[i] > mask_threshold)
        else:
            mask[i] = frame_mask

        # Get contours in frame
        cnts, hierarchy = cv2.findContours(frame_mask.astype('uint8'), cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        tmp = np.array([cv2.contourArea(x) for x in cnts])

        if tmp.size == 0:
            continue
        
        if kwargs.get('remove_obj_from_bg', False):
            # Object removal special case: deal with annoying large shadows
            enclosing_radii = np.array([cv2.minEnclosingCircle(x)[1] for x in cnts])
            radius_threshold = 50 # mouse encl. circle is no bigger than this
            possible_mice = enclosing_radii < radius_threshold
            try:
                mouse_cnt = np.where(tmp == tmp[possible_mice].max())[0][0]
            except:
                print(f'No acceptable contour found in frame {i}, skipping...')
                continue
        else:
            mouse_cnt = tmp.argmax()

        # if i == 50:
        #     pdb.set_trace()

        # Get features from contours
        for key, value in im_moment_features(cnts[mouse_cnt]).items():
            features[key][i] = value

    return features, mask


def crop_and_rotate_frames(frames, features, crop_size=(80, 80), progress_bar=False):
    '''
    Crops mouse from image and orients it s.t it is always facing east.

    Parameters
    ----------
    frames (3d np.ndarray): frames to crop and rotate
    features (dict): dict of extracted features, found in result_00.h5 files.
    crop_size (tuple): size of cropped image.
    progress_bar (bool): Display progress bar.
    gui (bool): indicate GUI is executing function

    Returns
    -------
    cropped_frames (3d np.ndarray): Crop and rotated frames.
    '''

    nframes = frames.shape[0]

    # Prepare cropped frame array
    cropped_frames = np.zeros((nframes, crop_size[0], crop_size[1]), frames.dtype)

    # Get window dimensions
    win = (crop_size[0] // 2, crop_size[1] // 2 + 1)
    border = (crop_size[1], crop_size[1], crop_size[0], crop_size[0])

    for i in tqdm(range(frames.shape[0]), disable=not progress_bar, desc='Rotating'):

        if np.any(np.isnan(features['centroid'][i])):
            continue

        # Get bounded frames
        use_frame = cv2.copyMakeBorder(frames[i], *border, cv2.BORDER_CONSTANT, 0)

        # Get row and column centroids
        rr = np.arange(features['centroid'][i, 1]-win[0],
                       features['centroid'][i, 1]+win[1]).astype('int16')
        cc = np.arange(features['centroid'][i, 0]-win[0],
                       features['centroid'][i, 0]+win[1]).astype('int16')

        rr = rr+crop_size[0]
        cc = cc+crop_size[1]

        # Ensure centroids are in bounded frame
        if (np.any(rr >= use_frame.shape[0]) or np.any(rr < 1)
                or np.any(cc >= use_frame.shape[1]) or np.any(cc < 1)):
            continue

        # Rotate the frame such that the mouse is oriented facing east
        rot_mat = cv2.getRotationMatrix2D((crop_size[0] // 2, crop_size[1] // 2),
                                          -np.rad2deg(features['orientation'][i]), 1)
        cropped_frames[i] = cv2.warpAffine(use_frame[rr[0]:rr[-1], cc[0]:cc[-1]],
                                           rot_mat, (crop_size[0], crop_size[1]))

    return cropped_frames


def compute_scalars(frames, track_features, min_height=10, max_height=100, true_depth=673.1):
    '''
    Computes scalars.

    Parameters
    ----------
    frames (3d np.ndarray): frames x r x c, uncropped mouse
    track_features (dict):  dictionary with tracking variables (centroid and orientation)
    min_height (float): minimum height of the mouse
    max_height (float): maximum height of the mouse
    true_depth (float): detected true depth

    Returns
    -------
    features (dict): dictionary of scalars
    '''

    nframes = frames.shape[0]

    # Pack features into dict
    features = {
        'centroid_x_px': np.zeros((nframes,), 'float32'),
        'centroid_y_px': np.zeros((nframes,), 'float32'),
        'velocity_2d_px': np.zeros((nframes,), 'float32'),
        'velocity_3d_px': np.zeros((nframes,), 'float32'),
        'width_px': np.zeros((nframes,), 'float32'),
        'length_px': np.zeros((nframes,), 'float32'),
        'area_px': np.zeros((nframes,)),
        'centroid_x_mm': np.zeros((nframes,), 'float32'),
        'centroid_y_mm': np.zeros((nframes,), 'float32'),
        'velocity_2d_mm': np.zeros((nframes,), 'float32'),
        'velocity_3d_mm': np.zeros((nframes,), 'float32'),
        'width_mm': np.zeros((nframes,), 'float32'),
        'length_mm': np.zeros((nframes,), 'float32'),
        'area_mm': np.zeros((nframes,)),
        'height_ave_mm': np.zeros((nframes,), 'float32'),
        'angle': np.zeros((nframes,), 'float32'),
        'velocity_theta': np.zeros((nframes,)),
    }

    # Get mm centroid
    centroid_mm = convert_pxs_to_mm(track_features['centroid'], true_depth=true_depth)
    centroid_mm_shift = convert_pxs_to_mm(track_features['centroid'] + 1, true_depth=true_depth)

    # Based on the centroid of the mouse, get the mm_to_px conversion
    px_to_mm = np.abs(centroid_mm_shift - centroid_mm)
    masked_frames = np.logical_and(frames > min_height, frames < max_height)

    features['centroid_x_px'] = track_features['centroid'][:, 0]
    features['centroid_y_px'] = track_features['centroid'][:, 1]

    features['centroid_x_mm'] = centroid_mm[:, 0]
    features['centroid_y_mm'] = centroid_mm[:, 1]

    # based on the centroid of the mouse, get the mm_to_px conversion

    features['width_px'] = np.min(track_features['axis_length'], axis=1)
    features['length_px'] = np.max(track_features['axis_length'], axis=1)
    features['area_px'] = np.sum(masked_frames, axis=(1, 2))

    features['width_mm'] = features['width_px'] * px_to_mm[:, 1]
    features['length_mm'] = features['length_px'] * px_to_mm[:, 0]
    features['area_mm'] = features['area_px'] * px_to_mm.mean(axis=1)

    features['angle'] = track_features['orientation']

    nmask = np.sum(masked_frames, axis=(1, 2))

    for i in range(nframes):
        if nmask[i] > 0:
            features['height_ave_mm'][i] = np.mean(
                frames[i, masked_frames[i]])

    vel_x = np.diff(np.concatenate((features['centroid_x_px'][:1], features['centroid_x_px'])))
    vel_y = np.diff(np.concatenate((features['centroid_y_px'][:1], features['centroid_y_px'])))
    vel_z = np.diff(np.concatenate((features['height_ave_mm'][:1], features['height_ave_mm'])))

    features['velocity_2d_px'] = np.hypot(vel_x, vel_y)
    features['velocity_3d_px'] = np.sqrt(
        np.square(vel_x)+np.square(vel_y)+np.square(vel_z))

    vel_x = np.diff(np.concatenate((features['centroid_x_mm'][:1], features['centroid_x_mm'])))
    vel_y = np.diff(np.concatenate((features['centroid_y_mm'][:1], features['centroid_y_mm'])))

    features['velocity_2d_mm'] = np.hypot(vel_x, vel_y)
    features['velocity_3d_mm'] = np.sqrt(
        np.square(vel_x)+np.square(vel_y)+np.square(vel_z))

    features['velocity_theta'] = np.arctan2(vel_y, vel_x)

    return features


def feature_hampel_filter(features, centroid_hampel_span=None, centroid_hampel_sig=3,
                          angle_hampel_span=None, angle_hampel_sig=3):
    '''
    Filters computed extraction features using Hampel Filtering.
    Used to detect and filter out outliers.

    Parameters
    ----------
    features (dict): dictionary of video features
    centroid_hampel_span (int): Centroid Hampel Span Filtering Kernel Size
    centroid_hampel_sig (int): Centroid Hampel Signal Filtering Kernel Size
    angle_hampel_span (int): Angle Hampel Span Filtering Kernel Size
    angle_hampel_sig (int): Angle Hampel Span Filtering Kernel Size

    Returns
    -------
    features (dict): filtered version of input dict.
    '''
    if centroid_hampel_span is not None and centroid_hampel_span > 0:
        padded_centroids = np.pad(features['centroid'],
                                  (((centroid_hampel_span // 2, centroid_hampel_span // 2)),
                                   (0, 0)),
                                  'constant', constant_values = np.nan)
        for i in range(1):
            vws = strided_app(padded_centroids[:, i], centroid_hampel_span, 1)
            med = np.nanmedian(vws, axis=1)
            mad = np.nanmedian(np.abs(vws - med[:, None]), axis=1)
            vals = np.abs(features['centroid'][:, i] - med)
            fill_idx = np.where(vals > med + centroid_hampel_sig * mad)[0]
            features['centroid'][fill_idx, i] = med[fill_idx]

        padded_orientation = np.pad(features['orientation'],
                                    (angle_hampel_span // 2, angle_hampel_span // 2),
                                    'constant', constant_values = np.nan)

    if angle_hampel_span is not None and angle_hampel_span > 0:
        vws = strided_app(padded_orientation, angle_hampel_span, 1)
        med = np.nanmedian(vws, axis=1)
        mad = np.nanmedian(np.abs(vws - med[:, None]), axis=1)
        vals = np.abs(features['orientation'] - med)
        fill_idx = np.where(vals > med + angle_hampel_sig * mad)[0]
        features['orientation'][fill_idx] = med[fill_idx]

    return features


def model_smoother(features, ll=None, clips=(-300, -125)):
    '''
    Spatial feature filtering.

    Parameters
    ----------
    features (dict): dictionary of extraction scalar features
    ll (np.array): list of loglikelihoods of pixels in frame
    clips (tuple): tuple to ensure video is indexed properly

    Returns
    -------
    features (dict) - smoothed version of input features
    '''

    if ll is None or clips is None or (clips[0] >= clips[1]):
        return features

    ave_ll = np.zeros((ll.shape[0], ))
    for i, ll_frame in enumerate(ll):

        max_mu = clips[1]
        min_mu = clips[0]

        smoother = np.mean(ll[i])
        smoother -= min_mu
        smoother /= (max_mu - min_mu)

        smoother = np.clip(smoother, 0, 1)
        ave_ll[i] = smoother

    for k, v in features.items():
        nans = np.isnan(v)
        ndims = len(v.shape)
        xvec = np.arange(len(v))
        if nans.any():
            if ndims == 2:
                for i in range(v.shape[1]):
                    f = scipy.interpolate.interp1d(xvec[~nans[:, i]], v[~nans[:, i], i],
                                                   kind='nearest', fill_value='extrapolate')
                    fill_vals = f(xvec[nans[:, i]])
                    features[k][xvec[nans[:, i]], i] = fill_vals
            else:
                f = scipy.interpolate.interp1d(xvec[~nans], v[~nans],
                                               kind='nearest', fill_value='extrapolate')
                fill_vals = f(xvec[nans])
                features[k][nans] = fill_vals

    for i in range(2, len(ave_ll)):
        smoother = ave_ll[i]
        for k, v in features.items():
            features[k][i] = (1 - smoother) * v[i - 1] + smoother * v[i]

    for i in reversed(range(len(ave_ll) - 1)):
        smoother = ave_ll[i]
        for k, v in features.items():
            features[k][i] = (1 - smoother) * v[i + 1] + smoother * v[i]

    return features
