import copy
import os
import shutil

# import warnings
import time
from functools import partial
from glob import glob
from multiprocessing import Pool, cpu_count
from os import path
from time import sleep

import charis
import dill as pickle
import matplotlib

matplotlib.use(backend='Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from astropy import units as u
from astropy.io import fits
from astropy.stats import mad_std, sigma_clip
from astropy.table import Table, setdiff, vstack
from astroquery.eso import Eso
from natsort import natsorted
from photutils.aperture import CircularAnnulus, CircularAperture
from tqdm import tqdm

from spherical.pipeline import flux_calibration, toolbox, transmission
from spherical.pipeline.toolbox import make_target_folder_string
from spherical.sphere_database.database_utils import find_nearest

# from spherical.sphere_database.database_utils import collect_reduction_infos


def convert_paths_to_filenames(full_paths):
    filenames = []
    for full_path in full_paths:
        extension = os.path.splitext(os.path.split(full_path)[-1])[1]
        if extension == '.Z':
            file_id = os.path.splitext(os.path.split(full_path)[-1])[0][:-5]
        else:
            file_id = os.path.splitext(os.path.split(full_path)[-1])[0]
        filenames.append(file_id)

    return filenames


def download_data_for_observation(raw_directory, observation, eso_username=None):
    if not os.path.exists(raw_directory):
        os.makedirs(raw_directory)

    if observation.filter == 'OBS_H' or observation.filter == 'OBS_YJ':
        raw_directory = os.path.join(raw_directory, 'IFS')
        print('Download attempted for IFS')
        science_keys = ['CORO', 'CENTER', 'FLUX']
        calibration_keys = [
            'WAVECAL']#, 'SPECPOS', 'BG_WAVECAL', 'BG_SPECPOS', 'FLAT']
    else:
        print('Download attempted for IRDIS')
        raw_directory = os.path.join(raw_directory, 'IRDIS')
        science_keys = ['CORO', 'CENTER', 'FLUX', 'BG_SCIENCE', 'BG_FLUX']
        calibration_keys = ['FLAT']  # 'DISTORTION'

    science_directory = os.path.join(raw_directory, 'science')
    if not os.path.exists(science_directory):
        os.makedirs(science_directory)

    target_name = observation.observation['MAIN_ID'][0]
    target_name = " ".join(target_name.split())
    target_name = target_name.replace(" ", "_")

    obs_band = observation.filter
    date = observation.observation['NIGHT_START'][0]

    target_directory = path.join(
        science_directory, target_name + '/' + obs_band + '/' + date)
    if not os.path.exists(target_directory):
        os.makedirs(target_directory)

    calibration_directory = os.path.join(raw_directory, 'calibration', obs_band)
    if not os.path.exists(calibration_directory):
        os.makedirs(calibration_directory)

    filenames = vstack(list(observation.frames.values()))['DP.ID']

    # Check for existing files
    existing_files = glob(os.path.join(raw_directory, '**', 'SPHER.*'), recursive=True)
    existing_files = convert_paths_to_filenames(existing_files)

    filename_table = Table({'DP.ID': filenames})
    existing_file_table = Table({'DP.ID': existing_files})
    if len(existing_file_table) > 0:
        download_list = setdiff(filename_table, existing_file_table, keys=['DP.ID'])
    else:
        download_list = filename_table

    if len(download_list) > 0:
        eso = Eso()
        if eso_username is not None:
            eso.login(username=eso_username)
        
        _ = eso.retrieve_data(
            datasets=list(download_list['DP.ID'].data),
            destination=raw_directory,
            with_calib=None,
            unzip=True)
    else:
        "No files to download."

    time.sleep(3)
    # Move downloaded files to proper directory
    for science_key in science_keys:
        files = list(observation.frames[science_key]['DP.ID']) #convert_paths_to_filenames(observation.frames[science_key]['FILE'])
        if len(files) > 0:
            destination_path = os.path.join(target_directory, science_key)
            if not os.path.exists(destination_path):
                os.makedirs(destination_path)
            for file in files:
                origin_path = os.path.join(raw_directory, file + '.fits')
                shutil.move(origin_path, destination_path)

    for calibration_key in calibration_keys:
        files = list(observation.frames[calibration_key]['DP.ID']) #convert_paths_to_filenames(observation.frames[calibration_key]['FILE'])
        if len(files) > 0:
            destination_path = os.path.join(calibration_directory, calibration_key)
            if not os.path.exists(destination_path):
                os.makedirs(destination_path)
            for file in files:
                origin_path = os.path.join(raw_directory, file + '.fits')
                shutil.move(origin_path, destination_path)
    return None


def bundle_output_into_cubes(key, cube_outputdir, output_type='resampled', overwrite=True):

    extracted_dir = path.join(cube_outputdir, key) + '/'
    converted_dir = path.join(cube_outputdir, 'converted') + '/'
    if not path.exists(converted_dir):
        os.makedirs(converted_dir)

    if output_type == 'resampled':
        glob_pattern = 'SPHER.*cube_resampled_DIT*.fits'
        name_suffix = ''
    elif output_type == 'hexagons':
        glob_pattern = 'SPHER.*cube_DIT*.fits'
        name_suffix = 'hexagons_'
    elif output_type == 'residuals':
        glob_pattern = 'SPHER.*cube_residuals_DIT*.fits'
        name_suffix = 'residuals_'
    else:
        raise ValueError('Invalid output_type selected.')

    science_files = natsorted(
        glob(os.path.join(extracted_dir, glob_pattern), recursive=False))

    if len(science_files) == 0:
        print('No output found in: {}'.format(extracted_dir))
        return None

    data_cube = []
    inverse_variance_cube = []
    parallactic_angles = []

    for file in science_files:
        hdus = fits.open(file)
        data_cube.append(hdus[1].data.astype('float32'))
        inverse_variance_cube.append(hdus[2].data.astype('float32'))
        try:
            parallactic_angles.append(
                hdus[0].header['HIERARCH DEROT ANGLE'])
        except Exception:
            print("Error retrieving parallactic angles for {}".format("CORO"))
        hdus.close()

    data_cube = np.array(data_cube, dtype='float32')
    inverse_variance_cube = np.array(inverse_variance_cube, dtype='float32')
    parallactic_angles = np.array(parallactic_angles)

    data_cube = np.swapaxes(data_cube, 0, 1)
    inverse_variance_cube = np.swapaxes(inverse_variance_cube, 0, 1)

    fits.writeto(os.path.join(converted_dir, '{}_{}cube.fits'.format(
        key, name_suffix).lower()), data_cube.astype('float32'), overwrite=overwrite)
    fits.writeto(os.path.join(converted_dir, '{}_{}ivar_cube.fits'.format(
        key, name_suffix).lower()), inverse_variance_cube.astype('float32'), overwrite=overwrite)
    if len(parallactic_angles) > 0:
        fits.writeto(os.path.join(converted_dir, '{}_parallactic_angles.fits'.format(
            key).lower()), parallactic_angles, overwrite=overwrite)


def execute_IFS_target(
        observation,
        calibration_parameters,
        extraction_parameters,
        reduction_parameters,
        reduction_directory,
        raw_directory=None,
        download_data=True,
        reduce_calibration=True,
        extract_cubes=False,
        frame_types_to_extract=['FLUX', 'CENTER', 'CORO'],
        bundle_output=True,
        bundle_hexagons=True,
        bundle_residuals=True,
        compute_frames_info=False,
        calibrate_centers=False,
        process_extracted_centers=False,
        calibrate_spot_photometry=False,
        calibrate_flux_psf=False,
        spot_to_flux=True,
        eso_username=None,
        overwrite=False,
        overwrite_calibration=False,
        overwrite_bundle=False,
        save_plots=True,
        verbose=True):

    start = time.time()

    if download_data:
        try:
            _ = download_data_for_observation(raw_directory=raw_directory, observation=observation, eso_username=eso_username)
        except:
            print('Download failed for observation: {}'.format(observation.observation['MAIN_ID'][0]))


    # name_mode_date = make_target_folder_string(observation)
 
    target_name = observation.observation['MAIN_ID'][0]
    target_name = " ".join(target_name.split())
    target_name = target_name.replace(" ", "_")
    obs_band = observation.observation['IFS_MODE'][0]
    date = observation.observation['NIGHT_START'][0]
    name_mode_date = target_name + '/' + obs_band + '/' + date
        
    outputdir = path.join(
        reduction_directory, 'IFS/observation', name_mode_date)

    if verbose:
        print(f"Start reduction of: {name_mode_date}")

    if not path.exists(outputdir):
        os.makedirs(outputdir)

    non_least_square_methods = ['optext', 'apphot3', 'apphot5']
    extraction_parameters['maxcpus'] = 1
    if obs_band == 'OBS_YJ':
        instrument = charis.instruments.SPHERE('YJ')
        extraction_parameters['R'] = 55
    elif obs_band == 'OBS_H':
        instrument = charis.instruments.SPHERE('YH')
        extraction_parameters['R'] = 35

    if (extraction_parameters['method'] in non_least_square_methods) \
            and extraction_parameters['linear_wavelength']:
        wavelengths = np.linspace(
            instrument.wavelength_range[0].value,
            instrument.wavelength_range[1].value,
            39)
    else:
        wavelengths = instrument.lam_midpts

    # if reduce_calibration or extract_cubes:
    # UPDATE FILE PATHS
    existing_file_paths = glob(os.path.join(
        raw_directory, '**', 'SPHER.*.fits'), recursive=True)
    existing_file_names = convert_paths_to_filenames(existing_file_paths)
    existing_files = pd.DataFrame({'name': existing_file_names})

    observation_orig = copy.deepcopy(observation)

    used_keys = ['CORO', 'CENTER', 'FLUX']  # 'BG_SCIENCE', 'BG_FLUX']  # , 'SPECPOS']
    if reduce_calibration:
        used_keys += ['WAVECAL']

    for key in used_keys:  # observation.frames.keys():
        try:
            observation.frames[key] = observation.frames[key].to_pandas()
            observation.frames[key]['FILE'] = observation.frames[key]['FILE'].str.decode(
                'UTF-8')
        except Exception:
            pass
        filepaths = []
        names_of_header_files = list(observation.frames[key]['DP.ID']) #convert_paths_to_filenames(observation.frames[key]['FILE'])
        for idx, name in enumerate(names_of_header_files):
            index_of_file = existing_files[existing_files['name'] == name].index.values[0]
            real_path_of_file = existing_file_paths[index_of_file]
            filepaths.append(real_path_of_file)
        observation.frames[key]['FILE'] = filepaths
    # else:
        # observation_orig = observation
    # observation_orig = copy.deepcopy(observation)
    # -------------------------------------------------------------------#
    # --------------------Wavelength calibration-------------------------#
    # -------------------------------------------------------------------#
    calibration_time_name = observation.frames['WAVECAL']['DP.ID'][0][6:]
    wavecal_outputdir = os.path.join(reduction_directory, 'IFS/calibration', obs_band, calibration_time_name)#, date)
    if reduce_calibration:
        # wavecal_outputdir = path.join(calibration_directory, 'calibration') + '/'
        if not path.exists(wavecal_outputdir):
            os.makedirs(wavecal_outputdir)

        files_in_calibration_folder = glob(os.path.join(wavecal_outputdir, '*key*.fits')) # TODO: This should check if oversanpling is used or not, different filename
        if len(files_in_calibration_folder) == 0 or overwrite_calibration:
            # check if reduction exists
            calibration_wavelength = instrument.calibration_wavelength
            wavecal_file = observation.frames['WAVECAL']['FILE'][0]
            inImage, hdr = charis.buildcalibrations.read_in_file(
                wavecal_file, instrument, calibration_wavelength,
                ncpus=calibration_parameters['ncpus'])
            # outdir=os.path.join(outputdir, 'calibration/')
            charis.buildcalibrations.buildcalibrations(
                inImage=inImage, instrument=instrument,
                inLam=calibration_wavelength.value,
                outdir=wavecal_outputdir,
                header=hdr,
                **calibration_parameters)

    # -------------------------------------------------------------------#
    # --------------------Cube extraction--------------------------------#
    # -------------------------------------------------------------------#

    cube_outputdir = path.join(outputdir, '{}'.format(
        extraction_parameters['method']))
    if not path.exists(cube_outputdir):
        os.makedirs(cube_outputdir)

    if extract_cubes:
        if extraction_parameters['bgsub'] and extraction_parameters['fitbkgnd']:
            raise ValueError('Background subtraction and fitting should not be used together.')

        # if observation.observation['WAFFLE_MODE'][0] == 'True':
        bgsub = extraction_parameters['bgsub']
        fitbkgnd = extraction_parameters['fitbkgnd']

        extraction_parameters['bgsub'] = bgsub
        extraction_parameters['fitbkgnd'] = fitbkgnd

        frame_type_to_dark_mapping = {
            'FLUX': 'SCIENCE',  # Don't use flux exposure time for background, because lower SNR for background compared to science frames
            'CORO': 'SCIENCE',
            'CENTER': 'SCIENCE',
            'WAVECAL': 'WAVECAL',
            'SPECPOS': 'SPECPOS'
        }

        if reduction_parameters['bg_pca']:
            bgpath = None
        else:
            if len(observation.background[frame_type_to_dark_mapping[key]]['SKY']['FILE']) > 0:
                bgpath = observation.frames['BG_SCIENCE']['FILE'].iloc[-1]
            else:
                print("No BG frame found to subtract. Falling back on PCA fit.")
                bgpath = None

        fitshift = extraction_parameters['fitshift']
        for key in frame_types_to_extract:
            if len(observation.frames[key]['FILE']) == 0:
                print("No files to reduce for key: {}".format(key))
                continue
            cube_type_outputdir = path.join(cube_outputdir, key) + '/'
            if not path.exists(cube_type_outputdir):
                os.makedirs(cube_type_outputdir)

            for idx, file in tqdm(enumerate(observation.frames[key]['FILE'])):
                hdr = fits.getheader(file)
                ndit = hdr['HIERARCH ESO DET NDIT']

                # raw_filename = os.path.splitext(os.path.basename(file))[0]
                # reduced_files = glob(os.path.join(cube_type_outputdir,
                #                      raw_filename) + '*resampled*fits')

                if key == 'CENTER':
                    if len(observation.frames['CORO']) > 0 and reduction_parameters['subtract_coro_from_center']:
                        extraction_parameters['bg_scaling_without_mask'] = True
                        print('center file!')
                        idx_nearest = find_nearest(
                            observation.frames['CORO'].iloc[:]['MJD_OBS'].values,
                            observation.frames['CENTER'].iloc[idx]['MJD_OBS'])
                        bg_frame = observation.frames['CORO']['FILE'].iloc[idx_nearest]
                    else:
                        extraction_parameters['bg_scaling_without_mask'] = False
                        bg_frame = None
                        print("PCA subtracting center frame BG.")
                else:
                    extraction_parameters['bg_scaling_without_mask'] = False
                    bg_frame = bgpath

                if key == 'FLUX':
                    # fitshift operates on spectra in the whole FoV
                    extraction_parameters['fitshift'] = False
                else:
                    extraction_parameters['fitshift'] = fitshift

                # if len(reduced_files) != ndit:
                if reduction_parameters['dit_cpus_max'] == 1:
                    for dit in tqdm(range(ndit)):
                        charis.extractcube.getcube(
                            filename=file,
                            dit=dit,
                            bgpath=bg_frame,
                            calibdir=wavecal_outputdir,
                            outdir=cube_type_outputdir,
                            **extraction_parameters
                        )
                else:
                    if ndit <= reduction_parameters['dit_cpus_max']:
                        ncpus = ndit
                    else:
                        ncpus = reduction_parameters['dit_cpus_max']

                    multiprocess_charis_ifs = partial(
                        charis.extractcube.getcube,
                        filename=file,
                        bgpath=bg_frame,
                        calibdir=wavecal_outputdir,
                        outdir=cube_type_outputdir,
                        **extraction_parameters)
                    # indices = range(ndit)           
                    # with Pool(processes=ncpus) as pool:  
                    #     for _ in tqdm(pool.imap(func=multiprocess_charis_ifs, iterable=indices), total=len(indices)):
                    #         pass
                    # Create a pool of workers
                    max_retries = 3
                    for i in range(max_retries):
                        try:
                            with Pool(processes=min(ncpus, cpu_count())) as pool:  
                                pool.map(func=multiprocess_charis_ifs, iterable=range(ndit))
                            break  # If the map call succeeds, break the loop
                        except BrokenPipeError:
                            print("A child process terminated abruptly, causing a BrokenPipeError.")
                            if i < max_retries - 1:  # No need to sleep on the last iteration
                                sleep(10)  # Wait for 10 seconds before retrying
                        except Exception as e:
                            print(f"An unexpected error occurred: {e}")
                            raise  # If an unexpected error occurs, break the loop

                extraction_parameters['bg_scaling_without_mask'] = False

    if bundle_output:
        for key in frame_types_to_extract:
            if bundle_hexagons:
                bundle_output_into_cubes(
                    key, cube_outputdir, output_type='hexagons', overwrite=overwrite)
            if bundle_residuals:
                bundle_output_into_cubes(
                    key, cube_outputdir, output_type='residuals', overwrite=overwrite)
            bundle_output_into_cubes(
                key, cube_outputdir, output_type='resampled', overwrite=overwrite)

        converted_dir = path.join(cube_outputdir, 'converted') + '/'

        fits.writeto(os.path.join(converted_dir, 'wavelengths.fits'),
                     wavelengths, overwrite=overwrite)

    """ FRAME INFO COMPUTATION """
    if compute_frames_info:
        converted_dir = path.join(cube_outputdir, 'converted') + '/'
        frames_info = {}
        for key in ['FLUX', 'CORO', 'CENTER']:
            if len(observation.frames[key]['DP.ID']) == 0:
                continue
            frames_info[key] = toolbox.prepare_dataframe(observation_orig.frames[key])
            toolbox.compute_times(frames_info[key])
            toolbox.compute_angles(frames_info[key])
            frames_info[key].to_csv(
                os.path.join(converted_dir, 'frames_info_{}.csv'.format(key.lower())))
            # except:
            #     print("Failed to compute angles for key: {}".format(key))

    """ DETERMINE CENTERS """
    if calibrate_centers:
        converted_dir = path.join(cube_outputdir, 'converted') + '/'
        center_cube = fits.getdata(os.path.join(converted_dir, 'center_cube.fits'))
        wavelengths = fits.getdata(os.path.join(converted_dir, 'wavelengths.fits'))
        plot_dir = os.path.join(converted_dir, 'center_plots/')
        if not path.exists(plot_dir):
            os.makedirs(plot_dir)

        # background fit only necessary when no CORO image is subtracted
        if len(observation.frames['CORO']['FILE']) == 0:
            fit_background = True
        else:
            fit_background = False

        # NOTE: waffle orientation missing in frames info??
        spot_centers, spot_distances, image_centers, spot_fit_amplitudes = toolbox.measure_center_waffle(
            cube=center_cube,
            wavelengths=wavelengths,
            waffle_orientation='x',
            frames_info=None,
            bpm_cube=None,
            outputdir=plot_dir,
            instrument='IFS',
            crop=False,
            crop_center=None,
            fit_background=fit_background,
            fit_symmetric_gaussian=True,
            high_pass=False)
        # save_plots=save_plots)

        fits.writeto(
            os.path.join(converted_dir, 'spot_centers.fits'), spot_centers, overwrite=overwrite)
        fits.writeto(
            os.path.join(converted_dir, 'spot_distances.fits'), spot_distances, overwrite=overwrite)
        fits.writeto(
            os.path.join(converted_dir, 'image_centers.fits'), image_centers, overwrite=overwrite)
        fits.writeto(
            os.path.join(converted_dir, 'spot_fit_amplitudes.fits'), spot_fit_amplitudes, overwrite=overwrite)

    if process_extracted_centers:
        plot_dir = os.path.join(converted_dir, 'center_plots/')
        if not path.exists(plot_dir):
            os.makedirs(plot_dir)
        spot_centers = fits.getdata(os.path.join(converted_dir, 'spot_centers.fits'))
        spot_distances = fits.getdata(os.path.join(converted_dir, 'spot_distances.fits'))
        image_centers = fits.getdata(os.path.join(converted_dir, 'image_centers.fits'))
        # spot_amplitudes = fits.getdata(os.path.join(converted_dir, 'spot_fit_amplitudes.fits'))
        center_cube = fits.getdata(os.path.join(converted_dir, 'center_cube.fits'))

        satellite_psf_stamps = toolbox.extract_satellite_spot_stamps(center_cube, spot_centers, stamp_size=57,
                                                                     shift_order=3, plot=False)
        master_satellite_psf_stamps = np.nanmean(np.nanmean(satellite_psf_stamps, axis=2), axis=1)
        fits.writeto(
            os.path.join(converted_dir, 'satellite_psf_stamps.fits'),
            satellite_psf_stamps.astype('float32'), overwrite=overwrite)
        fits.writeto(
            os.path.join(converted_dir, 'master_satellite_psf_stamps.fits'),
            master_satellite_psf_stamps.astype('float32'), overwrite=overwrite)

        # mean_spot_stamps = np.sum(satellite_psf_stamps, axis=2)
        # aperture_size = 3

        # stamp_size = [mean_spot_stamps.shape[-1], mean_spot_stamps.shape[-2]]
        # stamp_center = [mean_spot_stamps.shape[-1] // 2, mean_spot_stamps.shape[-2] // 2]
        # aperture = CircularAperture(stamp_center, aperture_size)
        #
        # psf_mask = aperture.to_mask(method='center')
        # # Make sure only pixels are used for which data exists
        # psf_mask = psf_mask.to_image(stamp_size) > 0
        # flux_sum_with_bg = np.sum(mean_spot_stamps[:, :, psf_mask], axis=2)
        #
        # bg_aperture = CircularAnnulus(stamp_center, r_in=aperture_size, r_out=aperture_size+2)
        # bg_mask = bg_aperture.to_mask(method='center')
        # bg_mask = bg_mask.to_image(stamp_size) > 0
        # area = np.pi*aperture_size**2
        # bg_flux = np.mean(mean_spot_stamps[:, :, bg_mask], axis=2) * area
        #
        # flux_sum_without_bg = flux_sum_with_bg - bg_flux

        if (extraction_parameters['method'] in non_least_square_methods) \
                and extraction_parameters['linear_wavelength'] is True:
            remove_indices = [0, 1, 21, 38]
        else:
            remove_indices = [0, 13, 19, 20]

        anomalous_centers_mask = np.zeros_like(wavelengths).astype('bool')
        anomalous_centers_mask[remove_indices] = True

        image_centers_fitted = np.zeros_like(image_centers)
        image_centers_fitted2 = np.zeros_like(image_centers)

        # wavelengths_clean = np.delete(wavelengths, remove_indices)
        # image_centers_clean = np.delete(image_centers, remove_indices, axis=0)

        coefficients_x_list = []
        coefficients_y_list = []

        for frame_idx in range(image_centers.shape[1]):
            good_wavelengths = ~anomalous_centers_mask & np.all(
                np.isfinite(image_centers[:, frame_idx]), axis=1)
            try:
                coefficients_x = np.polyfit(
                    wavelengths[good_wavelengths], image_centers[good_wavelengths, frame_idx, 0], deg=2)
                coefficients_y = np.polyfit(
                    wavelengths[good_wavelengths], image_centers[good_wavelengths, frame_idx, 1], deg=3)

                coefficients_x_list.append(coefficients_x)
                coefficients_y_list.append(coefficients_y)

                image_centers_fitted[:, frame_idx, 0] = np.poly1d(coefficients_x)(wavelengths)
                image_centers_fitted[:, frame_idx, 1] = np.poly1d(coefficients_y)(wavelengths)
            except:
                print("Failed first iteration polyfit for frame {}".format(frame_idx))
                image_centers_fitted[:, frame_idx, 0] = np.nan
                image_centers_fitted[:, frame_idx, 1] = np.nan

        for frame_idx in range(image_centers.shape[1]):
            if np.all(~np.isfinite(image_centers_fitted[:, frame_idx])):
                image_centers_fitted2[:, frame_idx, 0] = np.nan
                image_centers_fitted2[:, frame_idx, 1] = np.nan
            else:
                deviation = image_centers - image_centers_fitted
                filtered_data = sigma_clip(
                    deviation, axis=0, sigma=3, maxiters=None, cenfunc='median', stdfunc=mad_std, masked=True, copy=True)
                anomalous_centers_mask = np.any(filtered_data.mask, axis=2)
                anomalous_centers_mask[remove_indices] = True
                try:
                    coefficients_x = np.polyfit(
                        wavelengths[~anomalous_centers_mask[:, 1]],
                        image_centers[~anomalous_centers_mask[:, 1], frame_idx, 0], deg=2)
                    coefficients_y = np.polyfit(
                        wavelengths[~anomalous_centers_mask[:, 1]],
                        image_centers[~anomalous_centers_mask[:, 1], frame_idx, 1], deg=3)

                    coefficients_x_list.append(coefficients_x)
                    coefficients_y_list.append(coefficients_y)

                    image_centers_fitted2[:, frame_idx, 0] = np.poly1d(coefficients_x)(wavelengths)
                    image_centers_fitted2[:, frame_idx, 1] = np.poly1d(coefficients_y)(wavelengths)
                except:
                    print("Failed second iteration polyfit for frame {}".format(frame_idx))
                    image_centers_fitted2[:, frame_idx, 0] = np.nan
                    image_centers_fitted2[:, frame_idx, 1] = np.nan

            # plt.plot(wavelengths, image_centers[:, frame_idx, 0], label='data x')
            # plt.plot(wavelengths, image_centers_fitted[:, frame_idx, 0], label='model')
            # # plt.plot(wavelengths, image_centers_fitted2[:, frame_idx, 0], label='model iter')
            # plt.legend()
            # plt.show()
            #
            # plt.plot(wavelengths, image_centers[:, frame_idx, 1], label='data y')
            # plt.plot(wavelengths, image_centers_fitted[:, frame_idx, 1], label='model')
            # plt.plot(wavelengths, image_centers_fitted2[:, frame_idx, 1], label='model iter')
            # plt.legend()
            # plt.show()

        fits.writeto(os.path.join(converted_dir, 'image_centers_fitted.fits'),
                     image_centers_fitted, overwrite=overwrite)
        fits.writeto(os.path.join(converted_dir, 'image_centers_fitted_robust.fits'),
                     image_centers_fitted2, overwrite=overwrite)

        plt.close()
        n_wavelengths = image_centers.shape[0]
        n_frames = image_centers.shape[1]
        # colors = plt.cm.cool(np.linspace(0, 1, n_frames))
        colors = plt.cm.PiYG(np.linspace(0, 1, n_frames))

        fig = plt.figure(9)
        ax = fig.add_subplot(111)
        for frame_idx, color in enumerate(colors):
            ax.scatter(image_centers_fitted[:, frame_idx, 0], image_centers_fitted[:, frame_idx, 1],
                       s=np.linspace(20, 300, n_wavelengths), marker='o', color=color, label=str(frame_idx)+' (fitted)',
                       alpha=0.6)
            ax.scatter(image_centers_fitted2[:, frame_idx, 0], image_centers_fitted2[:, frame_idx, 1],
                       s=np.linspace(20, 300, n_wavelengths), marker='x', color=color, label=str(frame_idx)+' (fitted 2nd iter)',
                       alpha=0.9)
            ax.scatter(image_centers[:, frame_idx, 0], image_centers[:, frame_idx, 1],
                       s=np.linspace(20, 300, n_wavelengths), marker='+', color=color, label=str(frame_idx)+' (data)',
                       alpha=0.6)
        # plt.legend()
        # ax.set_aspect('equal')
        # plt.show()
        plt.savefig(os.path.join(plot_dir, 'center_evolution.pdf'), bbox_inches='tight')
        # plt.close()

    if calibrate_spot_photometry:
        converted_dir = path.join(cube_outputdir, 'converted') + '/'
        satellite_psf_stamps = fits.getdata(os.path.join(
            converted_dir, 'satellite_psf_stamps.fits')).astype('float64')
        # flux_variance = fits.getdata(os.path.join(converted_dir, 'flux_cube.fits'), 1)

        stamp_size = [satellite_psf_stamps.shape[-1], satellite_psf_stamps.shape[-2]]
        stamp_center = [satellite_psf_stamps.shape[-1] // 2, satellite_psf_stamps.shape[-2] // 2]

        # BG measurement
        bg_aperture = CircularAnnulus(stamp_center, r_in=15, r_out=18)
        bg_mask = bg_aperture.to_mask(method='center')
        bg_mask = bg_mask.to_image(stamp_size) > 0

        mask = np.ones_like(satellite_psf_stamps)
        mask[:, :, :, bg_mask] = 0
        ma_spot_stamps = np.ma.array(
            data=satellite_psf_stamps.reshape(
                satellite_psf_stamps.shape[0], satellite_psf_stamps.shape[1], satellite_psf_stamps.shape[2], -1),
            mask=mask.reshape(
                satellite_psf_stamps.shape[0], satellite_psf_stamps.shape[1], satellite_psf_stamps.shape[2], -1))

        sigma_clipped_array = sigma_clip(
            ma_spot_stamps,  # satellite_psf_stamps[:, :, :, bg_mask],
            sigma=3, maxiters=5, cenfunc=np.nanmedian, stdfunc=np.nanstd,
            axis=3, masked=True, return_bounds=False)

        bg_counts = np.ma.median(sigma_clipped_array, axis=3).data
        bg_std = np.ma.std(sigma_clipped_array, axis=3).data

        # BG correction of stamps
        bg_corr_satellite_psf_stamps = satellite_psf_stamps - bg_counts[:, :, :, None, None]

        fits.writeto(
            os.path.join(converted_dir, 'satellite_psf_stamps_bg_corrected.fits'),
            bg_corr_satellite_psf_stamps.astype('float32'), overwrite=overwrite)

        aperture = CircularAperture(stamp_center, 3)
        psf_mask = aperture.to_mask(method='center')
        # Make sure only pixels are used for which data exists
        psf_mask = psf_mask.to_image(stamp_size) > 0
        # ipsh()
        flux_sum = np.nansum(bg_corr_satellite_psf_stamps[:, :, :, psf_mask], axis=3)

        spot_snr = flux_sum / (bg_std * np.sum(psf_mask))

        master_satellite_psf_stamps_bg_corr = np.nanmean(
            np.nansum(bg_corr_satellite_psf_stamps, axis=2), axis=1)

        fits.writeto(
            os.path.join(converted_dir, 'spot_amplitudes.fits'),
            flux_sum, overwrite=overwrite)

        fits.writeto(
            os.path.join(converted_dir, 'spot_snr.fits'),
            spot_snr, overwrite=overwrite)

        fits.writeto(
            os.path.join(converted_dir, 'master_satellite_psf_stamps_bg_corr.fits'),
            master_satellite_psf_stamps_bg_corr.astype('float32'), overwrite=overwrite)

    """ FLUX FRAMES """
    if calibrate_flux_psf:
        converted_dir = path.join(cube_outputdir, 'converted') + '/'
        wavelengths = fits.getdata(
            os.path.join(converted_dir, 'wavelengths.fits'))
        flux_cube = fits.getdata(os.path.join(converted_dir, 'flux_cube.fits')).astype('float64')
        # flux_variance = fits.getdata(os.path.join(converted_dir, 'flux_cube.fits'), 1)
        plot_dir = os.path.join(converted_dir, 'flux_plots/')
        if not path.exists(plot_dir):
            os.makedirs(plot_dir)

        # flux_centers_guess_xy = np.zeros(
        #     [flux_cube.shape[0], flux_cube.shape[1], 1, 2])
        # flux_centers_guess_xy[:, :, :, 0] = 170.
        # flux_centers_guess_xy[:, :, :, 1] = 188.

        flux_centers = []
        flux_amplitudes = []
        guess_center_yx = []
        wave_median_flux_image = np.nanmedian(flux_cube[1:-1], axis=0)
        median_flux_image = np.nanmedian(wave_median_flux_image, axis=0)
        # for median_flux_image in wave_median_flux_image:
        #     guess_center_yx.append(np.unravel_index(
        #         np.nanargmax(median_flux_image), median_flux_image.shape))
        guess_center_yx = np.unravel_index(
            np.nanargmax(median_flux_image), median_flux_image.shape)
        for frame_number in range(flux_cube.shape[1]):
            data = flux_cube[:, frame_number]
            flux_center, flux_amplitude = toolbox.star_centers_from_PSF_img_cube(
                cube=data,
                wave=wavelengths,
                pixel=7.46,
                guess_center_yx=guess_center_yx,  # [frame_number],
                fit_background=False,
                fit_symmetric_gaussian=True,
                mask_deviating=False,
                deviation_threshold=0.8,
                mask=None,
                save_path=None)
            flux_centers.append(flux_center)
            flux_amplitudes.append(flux_amplitude)

        flux_centers = np.expand_dims(
            np.swapaxes(np.array(flux_centers), 0, 1),
            axis=2)
        flux_amplitudes = np.swapaxes(np.array(flux_amplitudes), 0, 1)
        fits.writeto(
            os.path.join(converted_dir, 'flux_centers.fits'), flux_centers, overwrite=overwrite)
        fits.writeto(
            os.path.join(converted_dir, 'flux_gauss_amplitudes.fits'), flux_amplitudes, overwrite=overwrite)

        flux_stamps = toolbox.extract_satellite_spot_stamps(
            flux_cube, flux_centers, stamp_size=57,
            shift_order=3, plot=False)
        # flux_variance_stamps = toolbox.extract_satellite_spot_stamps(
        #     flux_cube, flux_centers, stamp_size=57,
        #     shift_order=3, plot=False)
        fits.writeto(os.path.join(converted_dir, 'flux_stamps_uncalibrated.fits'),
                     flux_stamps.astype('float32'), overwrite=overwrite)

        # frames_info_coro = prepare_dataframe(observation_orig.frames['CORO'])
        # frames_info_center = prepare_dataframe(observation_orig.frames['CENTER'])
        # Adjust for exposure time and ND filter, put all frames to 1 second exposure
        # wave, bandwidth = transmission.wavelength_bandwidth_filter(filter_comb)
        if len(frames_info['FLUX']['INS4 FILT2 NAME'].unique()) > 1:
            raise ValueError('Non-unique ND filters in sequence.')
        else:
            ND = frames_info['FLUX']['INS4 FILT2 NAME'].unique()[0]

        _, attenuation = transmission.transmission_nd(ND, wave=wavelengths)
        fits.writeto(os.path.join(converted_dir, 'nd_attenuation.fits'),
                     attenuation, overwrite=overwrite)
        dits_flux = np.array(frames_info['FLUX']['DET SEQ1 DIT'])
        dits_center = np.array(frames_info['CENTER']['DET SEQ1 DIT'])
        # dits_coro = np.array(frames_info['CORO']['DET SEQ1 DIT'])

        unique_dits_center, unique_dits_center_counts = np.unique(dits_center, return_counts=True)

        # Normalize coronagraphic sequence to DIT that is most common
        if len(unique_dits_center) == 1:
            dits_factor = unique_dits_center[0] / dits_flux
            most_common_dit_center = unique_dits_center[0]
        else:
            most_common_dit_center = unique_dits_center[np.argmax(unique_dits_center_counts)]
            dits_factor = most_common_dit_center / dits_flux

        dit_factor_center = most_common_dit_center / dits_center

        fits.writeto(os.path.join(converted_dir, 'center_frame_dit_adjustment_factors.fits'),
                     dit_factor_center, overwrite=overwrite)

        print("Attenuation: {}".format(attenuation))
        # if adjust_dit:
        flux_stamps_calibrated = flux_stamps * dits_factor[None, :, None, None]

        flux_stamps_calibrated = flux_stamps_calibrated / \
            attenuation[:, np.newaxis, np.newaxis, np.newaxis]
        fits.writeto(os.path.join(converted_dir, 'flux_stamps_dit_nd_calibrated.fits'),
                     flux_stamps_calibrated, overwrite=overwrite)

        # fwhm_angle = ((wavelengths * u.nm) / (7.99 * u.m)).to(
        #     u.mas, equivalencies=u.dimensionless_angles())
        # fwhm = fwhm_angle.to(u.pixel, u.pixel_scale(0.00746 * u.arcsec / u.pixel)).value
        # aperture_sizes = np.round(fwhm * 2.1)

        flux_photometry = flux_calibration.get_aperture_photometry(
            flux_stamps_calibrated, aperture_radius_range=[1, 15],
            bg_aperture_inner_radius=15,
            bg_aperture_outer_radius=18)

        filehandler = open(os.path.join(converted_dir, 'flux_photometry.obj'), 'wb')
        pickle.dump(flux_photometry, filehandler)
        filehandler.close()

        fits.writeto(os.path.join(converted_dir, 'flux_amplitude_calibrated.fits'),
                     flux_photometry['psf_flux_bg_corr_all'], overwrite=overwrite)

        fits.writeto(os.path.join(converted_dir, 'flux_snr.fits'),
                     flux_photometry['snr_all'], overwrite=overwrite)

        plt.close()
        plt.plot(flux_photometry['aperture_sizes'], flux_photometry['snr_all'][:, :, 0])
        plt.xlabel('Aperture Size (pix)')
        plt.ylabel('BG limited SNR')
        plt.savefig(os.path.join(plot_dir, 'Flux_PSF_aperture_SNR.png'))
        plt.close()
        bg_sub_flux_stamps_calibrated = flux_stamps_calibrated - \
            flux_photometry['psf_bg_counts_all'][:, :, None, None]

        fits.writeto(os.path.join(converted_dir, 'flux_stamps_calibrated_bg_corrected.fits'),
                     bg_sub_flux_stamps_calibrated.astype('float32'), overwrite=overwrite)

        # bg_sub_flux_phot = get_aperture_photometry(
        #     bg_sub_flux_stamps_calibrated, aperture_radius_range=[1, 15],
        #     bg_aperture_inner_radius=15,
        #     bg_aperture_outer_radius=17)

        # Make master PSF
        flux_calibration_indices, indices_of_discontinuity = flux_calibration.get_flux_calibration_indices(
            frames_info['CENTER'], frames_info['FLUX'])
        flux_calibration_indices.to_csv(os.path.join(converted_dir, 'flux_calibration_indices.csv'))
        indices_of_discontinuity.tofile(os.path.join(
            converted_dir, 'indices_of_discontinuity.csv'), sep=',')

        # Normalize all PSF frames to respective calibration flux index based on aperture size 3 pixel
        # (index 2)

        number_of_flux_frames = flux_stamps.shape[1]
        flux_calibration_frames = []

        if reduction_parameters['flux_combination_method'] == 'mean':
            comb_func = np.nanmean
        elif reduction_parameters['flux_combination_method'] == 'median':
            comb_func = np.nanmedian
        else:
            raise ValueError('Unknown flux combination method.')


        for idx in range(len(flux_calibration_indices)):
            try:
                upper_range = flux_calibration_indices['flux_idx'].iloc[idx+1]
            except IndexError:
                upper_range = number_of_flux_frames

            if idx == 0:
                lower_index = 0
                lower_index_frame_combine = 0
                number_of_frames_to_combine = upper_range - lower_index
                if reduction_parameters['exclude_first_flux_frame'] and number_of_frames_to_combine > 1:
                    lower_index_frame_combine = 1               
            else:
                lower_index = flux_calibration_indices['flux_idx'].iloc[idx]
                lower_index_frame_combine = 0
                number_of_frames_to_combine = upper_range - lower_index
                if reduction_parameters['exclude_first_flux_frame_all'] and number_of_frames_to_combine > 1:
                    lower_index_frame_combine = 1

            phot_values = flux_photometry['psf_flux_bg_corr_all'][2][:, lower_index: upper_range]
            # Old way: pick closest in time
            # reference_value_old = flux_photometry['psf_flux_bg_corr_all'][2][:, flux_calibration_indices['flux_idx'].iloc[idx]]
            # New way do median
            reference_value = np.nanmean(
                flux_photometry['psf_flux_bg_corr_all'][2][:, lower_index_frame_combine:upper_range], axis=1)


            normalization_values = phot_values / reference_value[:, None]

            flux_calibration_frame = bg_sub_flux_stamps_calibrated[:,
                                                                   lower_index:upper_range] / normalization_values[:, :, None, None]
            # Could weight by snr, but let's not do that yet
            # snr_values = flux_photometry['snr_all'][2][:, lower_index:upper_range]
            # if reduction_parameters['exclude_first_flux_frame']:
            #     flux_calibration_frame = np.mean(flux_calibration_frame[:, 1:], axis=1)
            # else:
            flux_calibration_frame = comb_func(flux_calibration_frame[:, lower_index_frame_combine:], axis=1)
            flux_calibration_frames.append(flux_calibration_frame)

        flux_calibration_frames = np.array(flux_calibration_frames)
        flux_calibration_frames = np.swapaxes(flux_calibration_frames, 0, 1)

        fits.writeto(os.path.join(converted_dir, 'master_flux_calibrated_psf_frames.fits'),
                     flux_calibration_frames.astype('float32'), overwrite=overwrite)

        # # plt.plot(psf_flux_with_bg_all[:, 7, 7] / np.max(psf_flux_with_bg_all[:, 7, 7]))
        # median_norm_psf_aperture_flux = np.median(normalized_psf_aperture_flux, axis=2)
        # # plt.plot(normalized_psf_aperture_flux[:, :, 3])
        # plt.plot(median_norm_psf_aperture_flux[:, 3], color='red')
        # plt.plot(median_norm_psf_aperture_flux[:, 7], color='orange')
        # plt.plot(median_norm_psf_aperture_flux[:, 36], color='blue')

        # mean_flux_amplitude = np.mean(flux_amplitudes, axis=1)
        # flux_modulation = flux_amplitudes / mean_flux_amplitude[:, None]

    if spot_to_flux:
        converted_dir = path.join(cube_outputdir, 'converted') + '/'
        plot_dir = os.path.join(converted_dir, 'flux_plots/')
        if not path.exists(plot_dir):
            os.makedirs(plot_dir)

        wavelengths = fits.getdata(
            os.path.join(converted_dir, 'wavelengths.fits')) * u.nm
        wavelengths = wavelengths.to(u.micron)
        flux_amplitude = fits.getdata(
            os.path.join(converted_dir, 'flux_amplitude_calibrated.fits'))[2]

        spot_amplitude = fits.getdata(
            os.path.join(converted_dir, 'spot_amplitudes.fits'))
        master_spot_amplitude = np.mean(spot_amplitude, axis=2)

        psf_flux = flux_calibration.SimpleSpectrum(
            wavelength=wavelengths,
            flux=flux_amplitude,
            norm_wavelength_range=[1.0, 1.3] * u.micron,
            metadata=frames_info['FLUX'],
            rescale=False,
            normalize=False)

        # psf_flux_norm = flux_calibration.SimpleSpectrum(
        #     wavelength=wavelengths,
        #     flux=flux_amplitude,
        #     norm_wavelength_range=[1.0, 1.3] * u.micron,
        #     metadata=frames_info['FLUX'],
        #     rescale=False,
        #     normalize=True)

        spot_flux = flux_calibration.SimpleSpectrum(
            wavelength=wavelengths,
            flux=master_spot_amplitude,  # flux_sum_with_bg,
            norm_wavelength_range=[1.0, 1.3] * u.micron,
            metadata=frames_info['CENTER'],
            rescale=True,
            normalize=False)

        # spot_flux_norm = flux_calibration.SimpleSpectrum(
        #     wavelength=wavelengths,
        #     flux=master_spot_amplitude,  # flux_sum_with_bg,
        #     norm_wavelength_range=[1.0, 1.3] * u.micron,
        #     metadata=frames_info['CENTER'],
        #     rescale=True,
        #     normalize=True)

        psf_flux.plot_flux(plot_original=False, autocolor=True, cmap=plt.cm.cool,
                           savefig=True, savedir=plot_dir, filename='psf_flux.png',
                           )
        spot_flux.plot_flux(plot_original=False, autocolor=True, cmap=plt.cm.cool,
                            savefig=True, savedir=plot_dir, filename='spot_flux_rescaled.png',
                            )

        flux_calibration_indices = pd.read_csv(os.path.join(
            converted_dir, 'flux_calibration_indices.csv'))

        normalization_factors, averaged_normalization, std_dev_normalization = flux_calibration.compute_flux_normalization_factors(
            flux_calibration_indices, psf_flux, spot_flux)

        flux_calibration.plot_flux_normalization_factors(
            flux_calibration_indices, normalization_factors[:, 1:-1],
            wavelengths=wavelengths[1:-1], cmap=plt.cm.cool,
            savefig=True, savedir=plot_dir)

        fits.writeto(os.path.join(
            converted_dir, 'spot_normalization_factors.fits'), normalization_factors,
            overwrite=True)

        fits.writeto(os.path.join(
            converted_dir, 'spot_normalization_factors_average.fits'), averaged_normalization,
            overwrite=True)

        fits.writeto(os.path.join(
            converted_dir, 'spot_normalization_factors_stddev.fits'), std_dev_normalization,
            overwrite=True)

        flux_calibration.plot_timeseries(
            frames_info['FLUX'], frames_info['CENTER'], psf_flux, spot_flux, averaged_normalization,
            x_axis_quantity='HOUR ANGLE', wavelength_channels=np.arange(len(wavelengths))[1:-1],
            savefig=True, savedir=plot_dir)

        scaled_spot_flux = spot_flux.flux * averaged_normalization[:, None]
        temporal_mean = np.nanmean(scaled_spot_flux, axis=1)
        amplitude_variation = scaled_spot_flux / temporal_mean[:, None]

        fits.writeto(os.path.join(
            converted_dir, 'spot_amplitude_variation.fits'), amplitude_variation,
            overwrite=True)

    end = time.time()
    print((end - start) / 60.)

    return None

def output_directory_path(reduction_directory, observation, method='optext'):
    """
    Create the path for the final converted file directory.

    Parameters:
    reduction_directory (str): The path to the reduction directory.
    observation (Observation): An observation object.
    method (str, optional): The reduction method. Defaults to 'optext'.

    Returns:
    str: The path for the final converted file directory.
    """

    name_mode_date = make_target_folder_string(observation)
    outputdir = path.join(
        reduction_directory, 'IFS/observation', name_mode_date, f'{method}/converted/')

    return outputdir


def check_output(reduction_directory, observation_object_list, method='optext'):
    """
    Check if all required files are present in the output directory.

    Parameters:
    reduction_directory (str): The path to the reduction directory.
    observation_object_list (list): A list of observation objects.
    method (str, optional): The reduction method. Defaults to 'optext'.

    Returns:
    tuple: A tuple containing two lists. The first list contains boolean values indicating if the required files are present for each observation. The second list contains the missing files for each observation.
    """

    reduced = []
    missing_files_reduction = []

    for observation in observation_object_list:
        outputdir = output_directory_path(
            reduction_directory, 
            observation,
            method)
        
        files_to_check = [
            'wavelengths.fits',
            'coro_cube.fits',
            'center_cube.fits',
            'flux_stamps_calibrated_bg_corrected.fits',
            'frames_info_flux.csv',
            'frames_info_center.csv',
            'frames_info_coro.csv',
            'image_centers_fitted_robust.fits',
            'spot_amplitudes.fits',
        ]

        missing_files = []
        for file in files_to_check:
            if not path.isfile(path.join(outputdir, file)):
                missing_files.append(file)
        if len(missing_files) > 0:
            reduced.append(False)
        else:
            reduced.append(True)
        missing_files_reduction.append(missing_files)
    
    return reduced, missing_files_reduction