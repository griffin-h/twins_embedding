from astropy.table import Table
from hashlib import md5
from idrtools import Dataset, math
from matplotlib import pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from scipy.optimize import minimize
from sklearn.manifold import Isomap
import extinction
import numpy as np
import os
import sys
import tqdm

from settings import default_settings
import utils
import specind


class ManifoldTwinsException(Exception):
    pass


class ManifoldTwinsAnalysis:
    def __init__(self, **kwargs):
        """Load the dataset and setup the analysis"""

        # Update the default settings with any arguments that came in from kwargs.
        self.settings = dict(default_settings, **kwargs)

        self.print_verbose("Loading dataset...")
        self.print_verbose("IDR:          %s" % self.settings['idr'])
        self.print_verbose(
            "Phase range: [%.1f, %.1f]"
            % (-self.settings['phase_range'], self.settings['phase_range'])
        )
        self.print_verbose("Bin velocity: %.1f" % self.settings['bin_velocity'])

        self.dataset = Dataset.from_idr(
            os.path.join(self.settings['idr_directory'], self.settings['idr']),
            load_both_headers=True
        )

        # Do/load all of the SALT2 fits for this dataset
        self.dataset.load_salt_fits()

        all_raw_spec = []
        center_mask = []

        self.attrition_enough_spectra = 0
        self.attrition_salt_daymax = 0
        self.attrition_range = 0
        self.attrition_usable = 0

        for supernova in tqdm.tqdm(self.dataset.targets):
            if len(supernova.spectra) < 5:
                self.print_verbose(
                    "Cutting %s, not enough spectra to guarantee a "
                    "good LC fit" % supernova,
                    2,
                )
                continue
            self.attrition_enough_spectra += 1

            # Update the phases on the spectra
            supernova.reference_time += supernova.salt_fit['t0']

            daymax_err = supernova.salt_fit['t0_err']
            if daymax_err > 1.0:
                self.print_verbose(
                    "Cutting %s, day max err %.2f too high" % (supernova, daymax_err),
                    2,
                )
                continue
            self.attrition_salt_daymax += 1

            range_spectra = supernova.get_spectra_in_range(
                -self.settings['phase_range'], self.settings['phase_range']
            )
            if len(range_spectra) > 0:
                self.attrition_range += 1

            used_phases = []
            for spectrum in range_spectra:
                if self._check_spectrum(spectrum):
                    all_raw_spec.append(spectrum)
                    used_phases.append(spectrum.phase)
                else:
                    spectrum.usable = False

            used_phases = np.array(used_phases)
            if len(used_phases) > 0:
                # Figure out which spectrum was closest to the center of the
                # bin.
                self.attrition_usable += 1
                target_center_mask = np.zeros(len(used_phases), dtype=bool)
                target_center_mask[np.argmin(np.abs(used_phases))] = True
                center_mask.extend(target_center_mask)

        all_flux = []
        all_fluxerr = []
        all_spec = []

        for spectrum in all_raw_spec:
            bin_spec = spectrum.bin_by_velocity(
                self.settings['bin_velocity'],
                self.settings['bin_min_wavelength'],
                self.settings['bin_max_wavelength'],
            )
            all_flux.append(bin_spec.flux)
            all_fluxerr.append(bin_spec.fluxerr)
            all_spec.append(bin_spec)

        # All binned spectra have the same wavelengths, so save the wavelengths
        # from an arbitrary one of them.
        self.wave = all_spec[0].wave

        # Save the rest of the info
        self.flux = np.array(all_flux)
        self.fluxerr = np.array(all_fluxerr)
        self.raw_spectra = np.array(all_raw_spec)
        self.spectra = np.array(all_spec)
        self.center_mask = np.array(center_mask)

        # Pull out variables that we use all the time.
        self.helio_redshifts = self.read_meta("host.zhelio")
        self.redshifts = self.read_meta("host.zcmb")
        self.redshift_errs = self.read_meta("host.zhelio.err")

        # Build a list of targets and a map from spectra to targets.
        self.targets = np.unique([i.target for i in self.spectra])
        self.target_map = np.array(
            [self.targets.tolist().index(i.target) for i in self.spectra]
        )

        # Pull out SALT fit info
        self.salt_fits = Table([i.salt_fit for i in self.targets])
        self.salt_x1 = self.salt_fits['x1'].data
        self.salt_colors = self.salt_fits['c'].data
        self.salt_phases = np.array([i.phase for i in self.spectra])

        # Record which targets should be in the validation set.
        self.train_mask = np.array(
            [i["idr.subset"] != "validation" for i in self.targets]
        )

        # Build a hash that is unique to the dataset that we are working on.
        hash_info = (
            self.settings['idr']
            + ';' + str(self.settings['phase_range'])
            + ';' + str(self.settings['bin_velocity'])
            + ';' + str(self.settings['bin_min_wavelength'])
            + ';' + str(self.settings['bin_max_wavelength'])
            + ';' + str(self.settings['s2n_cut_min_wavelength'])
            + ';' + str(self.settings['s2n_cut_max_wavelength'])
            + ';' + str(self.settings['s2n_cut_threshold'])
        )
        self.dataset_hash = md5(hash_info.encode("ascii")).hexdigest()


    def _check_spectrum(self, spectrum):
        """Check if a spectrum is valid or not"""
        spectrum.do_lazyload()

        s2n_start = spectrum.get_signal_to_noise(
            self.settings['s2n_cut_min_wavelength'],
            self.settings['s2n_cut_max_wavelength'],
        )

        if s2n_start < self.settings['s2n_cut_threshold']:
            # Signal-to-noise cut. We find that a signal-to-noise of < ~100 in the
            # U-band leads to an added core dispersion of >0.1 mag in the U-band which
            # is much higher than expected from statistics. This is unacceptable for the
            # twins analysis that relies on getting the color right for a single
            # spectrum.
            self.print_verbose(
                "Cutting %s, start signal-to-noise %.2f "
                "too low." % (spectrum, s2n_start),
                2,
            )
            return False

        # We made it!
        return True

    def read_meta(self, key, center_only=True):
        """Read a key from the meta data of each spectrum/target

        This will first attempt to read the key in the spectrum object's meta
        data. If it isn't there, then it will try to read from the target
        instead.

        If center_only is True, then a single value is returned for each
        target, from the spectrum closest to the center of the range if
        applicable. Otherwise, the values will be returned for each spectrum in
        the sample.
        """
        if key in self.spectra[0].meta:
            read_spectrum = True
        elif key in self.spectra[0].target.meta:
            read_spectrum = False
        else:
            raise KeyError("Couldn't find key %s in metadata." % key)

        if center_only:
            use_spectra = self.spectra[self.center_mask]
        else:
            use_spectra = self.spectra

        res = []
        for spec in use_spectra:
            if read_spectrum:
                val = spec.meta[key]
            else:
                val = spec.target.meta[key]
            res.append(val)

        res = np.array(res)

        return res

    def print_verbose(self, message, minimum_verbosity=1):
        if self.settings['verbosity'] >= minimum_verbosity:
            print(message)

    def model_maximum_spectra(self, use_cache=True):
        """Estimate the spectra for each of our SNe Ia at maximum light.

        This algorithm uses all targets with multiple spectra to model the differential
        evolution of Type Ia supernovae near maximum light. This method does not rely on
        knowing the underlying model of Type Ia supernovae and only models the
        differences. The model is generated in magnitude space, so anything static in
        between us and the supernova, like dust, does not affect the model.

        The fit is performed using Stan. We only use Stan as a minimizer here,
        and we do some analytic tricks inside to speed up the computation. Don't try to
        run this in sampling model, the analytic tricks will mess up the uncertainties
        of a Bayesian analysis!

        If use_cache is True, then the fitted model will be retrieved from a
        cache if it exists. Make sure to run with use_cache=False if making
        modifications to the model!
        """
        # Load the stan model
        model_path = "./stan_models/phase_interpolation_analytic.stan"
        model_hash, model = utils.load_stan_model(
            model_path,
            verbosity=self.settings['verbosity']
        )

        # Build a hash that is unique to this dataset/analysis
        hash_info = (
            self.dataset_hash
            + ';' + model_hash
            + ';' + str(self.settings['maximum_num_phase_coefficients'])
        )
        self.maximum_hash = md5(hash_info.encode("ascii")).hexdigest()

        # If we ran this model before, read the cached result if we can.
        if use_cache:
            cache_result = utils.load_stan_result(self.maximum_hash)
            if cache_result is not None:
                # Found the cached result. Load it and don't redo the fit.
                self.maximum_result = cache_result
                self.maximum_flux = cache_result["maximum_flux"]
                self.maximum_fluxerr = cache_result["maximum_fluxerr"]
                return

        num_targets = len(self.targets)
        num_spectra = len(self.flux)
        num_wave = len(self.wave)
        num_phase_coefficients = self.settings['maximum_num_phase_coefficients']

        if num_phase_coefficients % 2 != 0:
            raise Exception("ERROR: Must have an even number of phase " "coefficients.")

        spectra_targets = [i.target for i in self.spectra]
        spectra_target_counts = np.array(
            [spectra_targets.count(i.target) for i in self.spectra]
        )

        phase_coefficients = np.zeros((num_spectra, num_phase_coefficients))

        for i, phase in enumerate(self.salt_phases):
            phase_scale = np.abs(
                (num_phase_coefficients / 2) * (phase / self.settings['phase_range'])
            )

            full_bins = int(np.floor(phase_scale))
            remainder = phase_scale - full_bins

            for j in range(full_bins + 1):
                if j == full_bins:
                    weight = remainder
                else:
                    weight = 1

                if phase > 0:
                    phase_bin = num_phase_coefficients // 2 + j
                else:
                    phase_bin = num_phase_coefficients // 2 - 1 - j

                phase_coefficients[i, phase_bin] = weight

        def stan_init():
            init_params = {
                "phase_slope": np.zeros(num_wave),
                "phase_quadratic": np.zeros(num_wave),
                "phase_slope_x1": np.zeros(num_wave),
                "phase_quadratic_x1": np.zeros(num_wave),
                "phase_dispersion_coefficients": (
                    0.01 * np.ones((num_phase_coefficients, num_wave))
                ),
                "gray_offsets": np.zeros(num_spectra),
                "gray_dispersion_scale": 0.02,
            }

            return init_params

        stan_data = {
            "num_targets": num_targets,
            "num_spectra": num_spectra,
            "num_wave": num_wave,
            "measured_flux": self.flux,
            "measured_fluxerr": self.fluxerr,
            "phases": [i.phase for i in self.spectra],
            "phase_coefficients": phase_coefficients,
            "num_phase_coefficients": num_phase_coefficients,
            "spectra_target_counts": spectra_target_counts,
            "target_map": self.target_map + 1,  # stan uses 1-based indexing
            "maximum_map": np.where(self.center_mask)[0] + 1,
            "salt_x1": self.salt_x1,
        }

        sys.stdout.flush()
        result = model.optimizing(
            data=stan_data, init=stan_init, verbose=True, iter=20000, history_size=100
        )

        self.maximum_result = result
        self.maximum_flux = result["maximum_flux"]
        self.maximum_fluxerr = result["maximum_fluxerr"]

        # Save the output to cache it for future runs.
        utils.save_stan_result(self.maximum_hash, result)

    def read_between_the_lines(self, use_cache=True):
        """Run the read between the lines algorithm.

        This algorithm estimates the brightnesses and colors of every spectrum
        and produces dereddened spectra.

        The fit is performed using Stan. We only use Stan as a minimizer here.
        """
        # Load the fiducial color law.
        self.rbtl_color_law = extinction.fitzpatrick99(
            self.wave, 1.0, self.settings['rbtl_fiducial_rv']
        )

        # Load the stan model
        model_path = "./stan_models/read_between_the_lines.stan"
        model_hash, model = utils.load_stan_model(
            model_path,
            verbosity=self.settings['verbosity']
        )

        # Build a hash that is unique to this dataset/analysis
        hash_info = (
            self.maximum_hash
            + ';' + model_hash
            + ';' + str(self.settings['rbtl_fiducial_rv'])
        )
        self.rbtl_hash = md5(hash_info.encode("ascii")).hexdigest()

        # If we ran this model before, read the cached result if we can.
        if use_cache:
            cache_result = utils.load_stan_result(self.rbtl_hash)
            if cache_result is not None:
                # Found the cached result. Load it and don't redo the fit.
                self._parse_rbtl_result(cache_result)
                return

        use_targets = self.targets

        num_targets = len(use_targets)
        num_wave = len(self.wave)

        def stan_init():
            # Use the spectrum closest to maximum as a first guess of the
            # target's spectrum.
            start_mean_flux = np.mean(self.maximum_flux, axis=0)
            start_fractional_dispersion = 0.1 * np.ones(num_wave)

            return {
                "mean_flux": start_mean_flux,
                "fractional_dispersion": start_fractional_dispersion,
                "colors_raw": np.zeros(num_targets - 1),
                "magnitudes_raw": np.zeros(num_targets - 1),
            }

        stan_data = {
            "num_targets": num_targets,
            "num_wave": num_wave,
            "maximum_flux": self.maximum_flux,
            "maximum_fluxerr": self.maximum_fluxerr,
            "color_law": self.rbtl_color_law,
        }

        sys.stdout.flush()
        result = model.optimizing(data=stan_data, init=stan_init, verbose=True,
                                  iter=5000)

        # Save the output to cache it for future runs.
        utils.save_stan_result(self.rbtl_hash, result)

        # Parse the result
        self._parse_rbtl_result(result)

    def _parse_rbtl_result(self, result):
        """Parse and save the result of a run of the RBTL analysis"""
        self.rbtl_result = result

        self.rbtl_colors = result["colors"]
        self.rbtl_mags = result["magnitudes"]
        self.mean_flux = result["mean_flux"]

        if self.settings['blinded']:
            # Immediately discard validation magnitudes so that we can't
            # accidentally look at them.
            self.rbtl_mags[~self.train_mask] = np.nan

        # Deredden the real spectra and set them to the same scale as the mean
        # spectrum.
        self.scale_flux = self.maximum_flux / result['model_scales']
        self.scale_fluxerr = self.maximum_fluxerr / result['model_scales']

    def build_masks(self):
        """Build masks that are used in the various manifold learning and magnitude
        analyses
        """
        # For the manifold learning analysis, we need to make sure that the spectra at
        # maximum light have reasonable uncertainties on their spectra at maximum light.
        # We define "reasonable" by comparing the variance of each spectrum to the
        # size of the intrinsic supernova variation measured in the RBTL analysis.
        intrinsic_dispersion = utils.frac_to_mag(
            self.rbtl_result["fractional_dispersion"]
        )
        intrinsic_power = np.sum(intrinsic_dispersion**2)
        maximum_uncertainty = utils.frac_to_mag(
            self.maximum_fluxerr / self.maximum_flux
        )
        maximum_power = np.sum(maximum_uncertainty**2, axis=1)
        self.maximum_uncertainty_fraction = maximum_power / intrinsic_power
        self.uncertainty_mask = (
            self.maximum_uncertainty_fraction <
            self.settings['mask_uncertainty_fraction']
        )
        self.print_verbose(
            "Masking %d/%d targets whose interpolation uncertainty power is "
            "more than %.3f of the intrinsic power."
            % (np.sum(~self.uncertainty_mask), len(self.uncertainty_mask),
               self.settings['mask_uncertainty_fraction'])
        )

        # Mask to select targets that have a magnitude that is expected to have a large
        # dispersion in brightness.
        with np.errstate(invalid="ignore"):
            self.redshift_color_mask = (
                (self.redshift_errs < 0.004)
                & (self.helio_redshifts > 0.02)
                & (self.rbtl_colors - np.median(self.rbtl_colors) < 0.5)
            )

    def generate_embedding(self):
        """Generate the manifold learning embedding."""
        self.isomap = Isomap(
            n_neighbors=self.settings['isomap_num_neighbors'],
            n_components=self.settings['isomap_num_components']
        )

        mask = self.uncertainty_mask
        self.isomap_diffs = self.scale_flux / self.mean_flux - 1

        self.embedding = utils.fill_mask(
            self.isomap.fit_transform(self.isomap_diffs[mask]), mask
        )

    def calculate_spectral_indicators(self):
        """Calculate spectral indicators for all of the features"""
        all_indicators = []

        for idx in range(len(self.scale_flux)):
            spec = specind.Spectrum(
                self.wave, self.scale_flux[idx], self.scale_fluxerr[idx] ** 2
            )
            indicators = spec.get_spin_dict()
            all_indicators.append(indicators)

        all_indicators = Table(all_indicators)

        self.spectral_indicators = all_indicators

    def _build_gp(self, x, yerr, hyperparameters=None, phase=False):
        """Build a george Gaussian Process object and kernels.
        """
        import george
        from george import kernels

        if hyperparameters is None:
            hyperparameters = self.gp_hyperparameters

        use_yerr = np.sqrt(yerr ** 2 + hyperparameters[1] ** 2 * np.ones(len(x)))

        ndim = x.shape[-1]
        if phase:
            use_dim = list(range(ndim - 1))
        else:
            use_dim = list(range(ndim))

        kernel = hyperparameters[2] ** 2 * kernels.Matern32Kernel(
            [hyperparameters[3] ** 2] * len(use_dim), ndim=ndim, axes=use_dim
        )

        if phase:
            # Additional kernel in phase direction.
            kernel += hyperparameters[4] ** 2 * kernels.Matern32Kernel(
                [hyperparameters[4] ** 2], ndim=ndim, axes=ndim - 1
            )

        gp = george.GP(kernel)
        gp.compute(x, use_yerr)

        return gp, hyperparameters[0]

    def _predict_gp(
        self,
        pred_x,
        pred_color,
        cond_x,
        cond_y,
        cond_yerr,
        cond_color,
        hyperparameters=None,
        return_cov=False,
        phase=False,
        **kwargs
    ):
        """Predict a Gaussian Process on the given data with a single shared
        length scale and assumed intrinsic dispersion
        """
        gp, color_slope = self._build_gp(
            cond_x, cond_yerr, hyperparameters, phase=phase
        )

        use_cond_y = cond_y - color_slope * cond_color

        pred = gp.predict(
            use_cond_y, np.atleast_2d(pred_x), return_cov=return_cov, **kwargs
        )

        if pred_color is not None:
            color_correction = pred_color * color_slope
            if isinstance(pred, tuple):
                # Have uncertainty too, add only to the predictions.
                pred = (pred[0] + color_correction, *pred[1:])
            else:
                # Only the predictions.
                pred += color_correction

        return pred

    def _predict_gp_oos(
        self,
        x,
        y,
        yerr,
        color,
        hyperparameters=None,
        condition_mask=None,
        return_var=False,
        phase=False,
        groups=None,
    ):
        """Do out-of-sample Gaussian Process predictions given hyperparameters

        A binary mask can be specified as condition_mask to specify a subset of
        the data to use for conditioning the GP. The predictions will be done
        on the full sample.
        """
        if np.isscalar(color):
            color = np.ones(len(x)) * color

        if condition_mask is None:
            cond_x = x
            cond_y = y
            cond_yerr = yerr
            cond_color = color
        else:
            cond_x = x[condition_mask]
            cond_y = y[condition_mask]
            cond_yerr = yerr[condition_mask]
            cond_color = color[condition_mask]

        # Do out-of-sample predictions for element in the condition sample.
        cond_preds = []
        cond_vars = []
        for idx in range(len(cond_x)):
            if groups is not None:
                match_idx = groups[idx] == groups
                del_x = cond_x[~match_idx]
                del_y = cond_y[~match_idx]
                del_yerr = cond_yerr[~match_idx]
                del_color = cond_color[~match_idx]
            else:
                del_x = np.delete(cond_x, idx, axis=0)
                del_y = np.delete(cond_y, idx, axis=0)
                del_yerr = np.delete(cond_yerr, idx, axis=0)
                del_color = np.delete(cond_color, idx, axis=0)
            pred = self._predict_gp(
                cond_x[idx],
                cond_color[idx],
                del_x,
                del_y,
                del_yerr,
                del_color,
                hyperparameters,
                return_var=return_var,
                phase=phase,
            )
            if return_var:
                cond_preds.append(pred[0][0])
                cond_vars.append(pred[1][0])
            else:
                cond_preds.append(pred[0])

        # Do standard predictions for elements that we aren't conditioning on.
        if condition_mask is None:
            all_preds = np.array(cond_preds)
            if return_var:
                all_vars = np.array(cond_vars)
        else:
            other_pred = self._predict_gp(
                x[~condition_mask],
                color[~condition_mask],
                cond_x,
                cond_y,
                cond_yerr,
                cond_color,
                hyperparameters,
                return_var=return_var,
                phase=phase,
            )
            all_preds = np.zeros(len(x))
            all_vars = np.zeros(len(x))

            if return_var:
                other_pred, other_vars = other_pred
                all_vars[condition_mask] = cond_vars
                all_vars[~condition_mask] = other_vars

            all_preds[condition_mask] = cond_preds
            all_preds[~condition_mask] = other_pred

        if return_var:
            return all_preds, all_vars
        else:
            return all_preds

    def get_peculiar_velocity_uncertainty(self, peculiar_velocity=300):
        """Calculate dispersion added to the magnitude due to host galaxy
        peculiar velocity
        """
        pec_vel_dispersion = (5 / np.log(10)) * (
            peculiar_velocity / 3e5 / self.redshifts
        )

        return pec_vel_dispersion

    def get_mags(self, kind="rbtl", full=False, peculiar_velocity=300):
        if kind == "rbtl":
            mags = self.rbtl_mags
            colors = self.rbtl_colors
            if full:
                mask = self.mag_mask & self.interp_mask
            else:
                mask = self.good_mag_mask & self.interp_mask
        elif kind == "salt" or kind == "salt_raw":
            if kind == "salt":
                mags = self.salt_hr
            elif kind == "salt_raw":
                mags = self.salt_hr_raw
            colors = self.salt_colors
            if full:
                mask = self.salt_mask & self.interp_mask
            else:
                mask = self.good_salt_mask & self.interp_mask
        else:
            raise ManifoldTwinsException("Unknown kind %s!" % kind)

        pec_vel_dispersion = self.get_peculiar_velocity_uncertainty(peculiar_velocity)

        use_mags = mags[mask]
        use_colors = colors[mask]
        use_embedding = self.embedding[mask]
        use_pec_vel_dispersion = pec_vel_dispersion[mask]

        return use_embedding, use_mags, use_colors, use_pec_vel_dispersion, mask

    def _calculate_gp_residuals(self, hyperparameters=None, kind="rbtl", **kwargs):
        """Calculate the GP prediction residuals for a set of
        hyperparameters
        """
        use_embedding, use_mags, use_colors, use_pec_vel, use_mask = self.get_mags(kind)

        preds = self._predict_gp_oos(
            use_embedding, use_mags, use_pec_vel, use_colors, hyperparameters, **kwargs
        )
        residuals = use_mags - preds
        return residuals

    def _calculate_gp_dispersion(self, hyperparameters=None, metric=np.std, **kwargs):
        """Calculate the GP dispersion for a set of hyperparameters"""
        return metric(self._calculate_gp_residuals(hyperparameters, **kwargs))

    def fit_gp(
        self, verbose=True, kind="rbtl", start_hyperparameters=[0.0, 0.05, 0.2, 5]
    ):
        """Fit a Gaussian Process to predict magnitudes for the data."""
        if verbose:
            print("Fitting GP hyperparameters...")

        good_embedding, good_mags, good_colors, good_pec_vel, good_mask = self.get_mags(
            kind
        )

        def to_min(x):
            gp, color_slope = self._build_gp(good_embedding, good_pec_vel, x)
            residuals = good_mags - good_colors * color_slope
            return -gp.log_likelihood(residuals)

        res = minimize(to_min, start_hyperparameters)
        if verbose:
            print("Fit result:")
            print(res)
        self.gp_hyperparameters = res.x

        full_embedding, full_mags, full_colors, full_pec_vel, full_mask = self.get_mags(
            kind, full=True
        )

        preds, pred_vars = self._predict_gp_oos(
            full_embedding,
            full_mags,
            full_pec_vel,
            full_colors,
            condition_mask=good_mask[full_mask],
            return_var=True,
        )

        self.corr_mags = fill_mask(full_mags - preds, full_mask)
        self.corr_vars = fill_mask(pred_vars + full_pec_vel ** 2, full_mask)
        good_corr_mags = self.corr_mags[good_mask]

        # Calculate the parameter covariance. I use a code that I wrote for my
        # scene_model package for this, which won't always be available... I
        # should probably break that out into something different.
        try:
            from scene_model import calculate_covariance_finite_difference

            param_names = ["param_%d" for i in range(len(self.gp_hyperparameters))]

            def chisq(x):
                # My covariance algorithm requires a "chi-square" which isn't
                # really a chi-square any more. It is a negative log-likelihood
                # times 2... I really need to put this in some separate package
                # thing.
                return to_min(x) * 2

            cov = calculate_covariance_finite_difference(
                chisq,
                param_names,
                self.gp_hyperparameters,
                [(None, None)] * len(param_names),
                verbose=verbose,
            )

            self.gp_hyperparameter_covariance = cov
            if verbose:
                print(
                    "Fit uncertainty:",
                    np.sqrt(np.diag(self.gp_hyperparameter_covariance)),
                )
        except ModuleNotFoundError:
            print(
                "WARNING: scene_model not available, so couldn't calculate "
                "hyperparameter covariance."
            )
            pass

        if verbose:
            print("Fit NMAD:       ", math.nmad(good_corr_mags))
            print("Fit std:        ", np.std(good_corr_mags))

    def apply_gp_standardization(
        self, verbose=True, hyperparameters=None, phase=False, kind="rbtl"
    ):
        """Use a Gaussian Process to predict magnitudes for the data.

        If hyperparameters is specified, then the hyperparameters are used
        directly. Otherwise, the hyperparameters are fit to the data.
        """
        if hyperparameters is None:
            print("Fitting GP hyperparameters...")

            def to_min(x):
                return self._calculate_gp_dispersion(x, phase=phase, kind=kind)

            res = minimize(to_min, [0.1, 0.3, 5])
            print("Fit result:")
            print(res)
            self.gp_hyperparameters = res.x
        else:
            print("Using fixed GP hyperparameters...")
            self.gp_hyperparameters = hyperparameters

        good_embedding, good_mags, good_colors, good_pec_vel, good_mask = self.get_mags(
            kind
        )
        full_embedding, full_mags, full_colors, full_pec_vel, full_mask = self.get_mags(
            kind, full=True
        )

        preds = self._predict_gp_oos(
            full_embedding,
            full_mags,
            full_pec_vel,
            full_colors,
            condition_mask=good_mask[full_mask],
            phase=phase,
        )

        self.corr_mags = fill_mask(full_mags - preds, full_mask)
        good_corr_mags = self.corr_mags[good_mask]

        print("Fit NMAD:       ", math.nmad(good_corr_mags))
        print("Fit std:        ", np.std(good_corr_mags))

    def predict_gp(self, x, colors=None, hyperparameters=None, kind="rbtl", **kwargs):
        """Do the GP prediction at specific points using the full GP
        conditioning.

        Note: this function uses all of the training data to make predictions.
        Use _predict_gp_oos or something similar to properly do out of sample
        predictions if you want to predict on the training data.
        """
        use_embedding, use_mags, use_colors, use_pec_vel, use_mask = self.get_mags(kind)

        preds = self._predict_gp(
            x,
            colors,
            use_embedding,
            use_mags,
            use_pec_vel,
            use_colors,
            hyperparameters,
            **kwargs
        )

        return preds

    def plot_gp(
        self,
        axis_1=0,
        axis_2=1,
        hyperparameters=None,
        num_points=50,
        border=0.5,
        marker_size=60,
        vmin=-0.2,
        vmax=0.2,
        kind="rbtl",
        cmap=plt.cm.coolwarm,
    ):
        """Plot the GP predictions with data overlayed."""
        use_embedding, use_mags, use_colors, use_pec_vel, use_mask = self.get_mags(kind)

        # Apply the color correction to the magnitudes.
        if hyperparameters is None:
            color_slope = self.gp_hyperparameters[0]
        else:
            color_slope = hyperparameters[0]

        use_mags = use_mags - color_slope * use_colors

        x = use_embedding[:, axis_1]
        y = use_embedding[:, axis_2]

        min_x = np.nanmin(x) - border
        max_x = np.nanmax(x) + border
        min_y = np.nanmin(y) - border
        max_y = np.nanmax(y) + border

        plot_x, plot_y = np.meshgrid(
            np.linspace(min_x, max_x, num_points), np.linspace(min_y, max_y, num_points)
        )

        flat_plot_x = plot_x.flatten()
        flat_plot_y = plot_y.flatten()

        plot_coords = np.zeros((len(flat_plot_x), self.embedding.shape[1]))

        plot_coords[:, axis_1] = flat_plot_x
        plot_coords[:, axis_2] = flat_plot_y

        pred = self.predict_gp(plot_coords, hyperparameters=hyperparameters, kind=kind)
        pred = pred.reshape(plot_x.shape)

        self.scatter(
            fill_mask(use_mags, use_mask),
            mask=use_mask,
            label="Residual magnitude",
            axis_1=axis_1,
            axis_2=axis_2,
            vmin=vmin,
            vmax=vmax,
            cmap=cmap,
            invert_colorbar=True,
            edgecolors="k",
            marker_size=marker_size,
        )

        plt.imshow(
            pred[::-1],
            extent=(min_x, max_x, min_y, max_y),
            cmap=plt.cm.coolwarm.reversed(),
            vmin=vmin,
            vmax=vmax,
            aspect="auto",
        )

        plt.tight_layout()

    def load_host_data(self):
        """Load host data from Rigault et al. 2019"""
        host_data = Table.read(
            "./data/host_properties_rigault_2019.txt", format="ascii"
        )
        all_host_idx = []
        host_mask = []
        for target in self.targets:
            name = target.name
            match = host_data["name"] == name

            # Check if found
            if not np.any(match):
                host_mask.append(False)
                continue

            # row = host_data[match][0]
            all_host_idx.append(np.where(match)[0][0])
            host_mask.append(True)

        # Save the loaded data
        self.host_mask = np.array(host_mask)
        fill_host_data = fill_mask(host_data[all_host_idx].as_array(), self.host_mask)
        self.host_data = Table(fill_host_data, names=host_data.columns)

    def plot_host_variable(
        self, variable, mask=None, mag_type="rbtl", match_masks=False, threshold=None
    ):
        """Plot diagnostics for some host variable.

        Valid variable names are the keys in the host_data Table that comes
        from load_host_data.

        mag_type selects which magnitudes to plot. The options are:
        - rbtl: use the RBTL magnitudes (default)
        - salt: use the SALT2 corrected Hubble residuals

        If match_masks is True, then the masks required for both the Isomap
        manifold and SALT2 are applied (leaving a smaller dataset).
        """
        if mask is None:
            mask = np.ones(len(self.targets), dtype=bool)

        if match_masks:
            mag_mask = mask & self.good_salt_mask & self.good_mag_mask
        elif mag_type == "rbtl":
            mag_mask = mask & self.good_mag_mask
        elif mag_type == "salt":
            mag_mask = mask & self.good_salt_mask

        if mag_type == "rbtl":
            host_corr_mags = self.corr_mags
        elif mag_type == "salt":
            host_corr_mags = self.salt_hr

        host_mask = self.host_mask

        # Find a threshold for a split
        host_values = np.squeeze(self.host_data[variable])

        if threshold is None:
            threshold = np.nanmedian(host_values)

        with np.errstate(invalid="ignore"):
            host_cut_1 = host_mask & (host_values < threshold)
            host_cut_2 = host_mask & (host_values > threshold)

        host_mags_1 = host_corr_mags[host_cut_1 & mag_mask]
        host_mags_2 = host_corr_mags[host_cut_2 & mag_mask]

        mean_diff = np.nanmean(host_mags_1) - np.nanmean(host_mags_2)
        mean_diff_err = np.sqrt(
            np.var(host_mags_1) / len(host_mags_1)
            + np.var(host_mags_2) / len(host_mags_2)
        )

        print("Threshold:   %.3f" % threshold)
        print("Mean diff:   %.4f ± %.4f mag" % (mean_diff, mean_diff_err))
        print(
            "Median diff: %.4f mag"
            % (np.nanmedian(host_mags_1) - np.nanmedian(host_mags_2))
        )

        plt.figure(figsize=(8, 8))
        plt.title(variable)
        plt.subplot(2, 2, 1)
        plt.scatter(
            self.embedding[:, 0],
            self.embedding[:, 1],
            c=host_values,
            cmap=plt.cm.coolwarm,
            vmin=threshold - 0.1,
            vmax=threshold + 0.1,
        )
        plt.xlabel("Isomap parameter 0")
        plt.ylabel("Isomap parameter 1")

        plt.subplot(2, 2, 2)
        plt.scatter(
            host_values, host_corr_mags, s=2 + 30 * mag_mask, cmap=plt.cm.coolwarm
        )
        plt.xlabel(variable)
        plt.ylabel("Corrected mags")

        plt.subplot(2, 2, 3)
        plt.scatter(host_values, self.embedding[:, 1], c=self.embedding[:, 0])
        plt.xlabel(variable)
        plt.ylabel("Isomap parameter 1")

        plt.subplot(2, 2, 4)
        plt.scatter(host_values, self.embedding[:, 0], c=self.embedding[:, 1])
        plt.xlabel(variable)
        plt.ylabel("Isomap parameter 0")

        plt.tight_layout()

    def plot_host(self, threshold=None, mask=None):
        """Make an interactive plot of host properties"""
        from ipywidgets import interact, fixed

        interact(
            self.plot_host_variable,
            variable=self.host_data.keys()[1:],
            mask=fixed(mask),
            threshold=fixed(threshold),
            mag_type=["rbtl", "salt"],
        )

    def plot_distances(self):
        """Plot the reconstructed distances from the embedding against the true
        distances
        """
        from scipy.spatial.distance import pdist

        mask = self.interp_mask

        spec_dists = pdist(self.iso_diffs[mask])
        embedding_dists = pdist(self.embedding[mask])

        plt.figure()
        plt.scatter(
            spec_dists,
            embedding_dists * np.median(spec_dists) / np.median(embedding_dists),
            s=1,
            alpha=0.1,
        )
        plt.xlabel("Twins distance")
        plt.ylabel("Scaled transformed distance")

    def plot_twin_distances(self, twins_percentile=10, figsize=None):
        """Plot a histogram of where twins show up in the transformed
        embedding.
        """
        from IPython.display import display
        from scipy.spatial.distance import pdist
        from scipy.stats import percentileofscore
        import pandas as pd

        mask = self.interp_mask

        spec_dists = pdist(self.iso_diffs[mask])
        embedding_dists = pdist(self.embedding[mask])

        splits = {
            "Best 10% of twinness": (0, 10),
            "10-20%": (10, 20),
            "20-50%": (20, 50),
            "Worst 50% of twinness": (50, 100),
        }

        # Set weight so that the histogram is 1 if we have every element in
        # that bin.
        weight = 100 / len(embedding_dists)

        all_percentiles = []
        all_weights = []

        all_spec_cuts = []
        all_embedding_cuts = []

        for label, (min_percentile, max_percentile) in splits.items():
            spec_cut = (spec_dists >= np.percentile(spec_dists, min_percentile)) & (
                spec_dists < np.percentile(spec_dists, max_percentile)
            )
            embedding_cut = (embedding_dists >= np.percentile(embedding_dists, min_percentile)) & (
                embedding_dists < np.percentile(embedding_dists, max_percentile)
            )
            percentiles = []
            for dist in embedding_dists[spec_cut]:
                percentiles.append(percentileofscore(embedding_dists, dist))
            percentiles = np.array(percentiles)
            weights = np.ones(len(percentiles)) * weight

            all_percentiles.append(percentiles)
            all_weights.append(weights)
            all_spec_cuts.append(spec_cut)
            all_embedding_cuts.append(embedding_cut)

        plt.figure(figsize=figsize)
        plt.hist(
            all_percentiles,
            100,
            (0, 100),
            weights=all_weights,
            histtype="barstacked",
            label=splits.keys(),
        )
        plt.xlabel("Recovered twinness percentile in the embedded space")
        plt.ylabel("Fraction in bin")
        plt.legend()

        plt.xlim(0, 100)
        plt.ylim(0, 1)

        for label, (min_percentile, max_percentile) in splits.items():
            plt.axvline(max_percentile, c="k", lw=2, ls="--")

        # Build leakage matrix.
        leakage_matrix = np.zeros((len(splits), len(splits)))
        for idx_1, label_1 in enumerate(splits.keys()):
            for idx_2, label_2 in enumerate(splits.keys()):
                spec_cut = all_spec_cuts[idx_1]
                embedding_cut = all_embedding_cuts[idx_2]
                leakage = np.sum(embedding_cut & spec_cut) / np.sum(spec_cut)
                leakage_matrix[idx_1, idx_2] = leakage

        # Print the leakage matrix using pandas
        df = pd.DataFrame(
            leakage_matrix,
            index=["From %s" % i for i in splits.keys()],
            columns=["To %s" % i for i in splits.keys()],
        )
        display(df)

        return leakage_matrix

    def plot_twin_pairings(self, mask=None, show_nmad=False):
        """Plot the twins delta M as a function of twinness ala Fakhouri"""
        from scipy.spatial.distance import pdist
        from scipy.stats import percentileofscore

        if mask is None:
            mask = self.good_mag_mask

        use_spec = self.iso_diffs[mask]
        use_mag = self.rbtl_mags[mask]

        use_mag -= np.mean(use_mag)

        spec_dists = pdist(use_spec)
        delta_mags = pdist(use_mag[:, None])

        percentile = np.array([percentileofscore(spec_dists, i) for i in spec_dists])

        mags_20 = delta_mags[percentile < 20]
        self.twins_rms = math.rms(mags_20) / np.sqrt(2)
        self.twins_nmad = math.nmad_centered(mags_20) / np.sqrt(2)

        print("RMS  20%:", self.twins_rms)
        print("NMAD 20%:", self.twins_nmad)

        plt.figure()
        math.plot_binned_rms(
            percentile,
            delta_mags / np.sqrt(2),
            bins=20,
            label="RMS",
            equal_bin_counts=True,
        )
        if show_nmad:
            math.plot_binned_nmad_centered(
                percentile,
                delta_mags / np.sqrt(2),
                bins=20,
                label="NMAD",
                equal_bin_counts=True,
            )

        plt.xlabel("Twinness percentile")
        plt.ylabel("Single supernova dispersion in brightness (mag)")

        plt.legend()

        return mags_20

    def _evaluate_salt_hubble_residuals(
        self, MB, alpha, beta, intrinsic_dispersion, peculiar_velocity_uncertainty
    ):
        """Evaluate SALT Hubble residuals for a given set of standardization
        parameters

        Parameters
        ==========
        MB : float
            The intrinsic B-band brightness of Type Ia supernovae
        alpha : float
            Standardization coefficient for the SALT2 x1 parameter
        beta : float
            Standardization coefficient for the SALT2 color parameter
        intrinsic_dispersion : float
            Assumed intrinsic dispersion of the sample.

        Returns
        =======
        residuals : numpy.array
            The Hubble residuals for every target in the dataset
        residual_uncertainties : numpy.array
            The uncertainties on the Hubble residuals for every target in the
            dataset.
        """
        mb = -2.5*np.log10(self.salt_fits['x0'].data)
        x0_err = self.salt_fits['x0_err'].data
        mb_err = frac_to_mag(x0_err / self.salt_fits['x0'].data)
        x1_err = self.salt_fits['x1_err'].data
        color_err = self.salt_fits['c_err'].data

        cov_mb_x1 = self.salt_fits['covariance'].data[:, 1, 2] * -mb_err / x0_err
        cov_color_mb = self.salt_fits['covariance'].data[:, 1, 3] * -mb_err / x0_err
        cov_color_x1 = self.salt_fits['covariance'].data[:, 2, 3]

        residuals = (
            mb
            - MB
            + alpha * self.salt_x1
            - beta * self.salt_colors
        )

        residual_uncertainties = np.sqrt(
            intrinsic_dispersion ** 2
            + peculiar_velocity_uncertainty ** 2
            + mb_err ** 2
            + alpha ** 2 * x1_err ** 2
            + beta ** 2 * color_err ** 2
            + 2 * alpha * cov_mb_x1
            - 2 * beta * cov_color_mb
            - 2 * alpha * beta * cov_color_x1
        )

        return residuals, residual_uncertainties

    def calculate_salt_hubble_residuals(self, peculiar_velocity=300):
        """Calculate SALT hubble residuals"""
        # For SALT, can only use SNe that are in the good sample
        self.salt_mask = np.array(
            [i["idr.subset"] in ["training", "validation"] for i in self.targets]
        )

        # We also require reasonable redshifts and colors for the determination
        # of standardization parameters. The redshift_color_mask produced by
        # the read_between_the_lines algorithm does this.
        self.good_salt_mask = self.salt_mask & self.redshift_color_mask

        # Get the uncertainty due to peculiar velocities
        pec_vel_disp = self.get_peculiar_velocity_uncertainty(peculiar_velocity)

        mask = self.good_salt_mask

        # Starting value for intrinsic dispersion. We will update this in each
        # round to set chi2 = 1
        intrinsic_dispersion = 0.1

        for i in range(5):
            def calc_dispersion(MB, alpha, beta, intrinsic_dispersion):
                residuals, residual_uncertainties = self._evaluate_salt_hubble_residuals(
                    MB, alpha, beta, intrinsic_dispersion, pec_vel_disp
                )

                mask_residuals = residuals[mask]
                mask_residual_uncertainties = residual_uncertainties[mask]

                weights = 1 / mask_residual_uncertainties ** 2

                dispersion = np.sqrt(
                    np.sum(weights * mask_residuals ** 2)
                    / np.sum(weights)
                    # / ((len(mask_residuals) - 1) / len(mask_residuals))
                )

                return dispersion

            def to_min(x):
                return calc_dispersion(*x, intrinsic_dispersion)

            res = minimize(to_min, [-10, 0.13, 3.0])

            wrms = res.fun

            MB, alpha, beta = res.x

            print("Pass %d, MB=%.3f, alpha=%.3f, beta=%.3f" % (i, MB, alpha, beta))

            # Reestimate intrinsic dispersion.
            def chisq(intrinsic_dispersion):
                residuals, residual_uncertainties = self._evaluate_salt_hubble_residuals(
                    MB, alpha, beta, intrinsic_dispersion, pec_vel_disp
                )

                mask_residuals = residuals[mask]
                mask_residual_uncertainties = residual_uncertainties[mask]

                return np.sum(
                    mask_residuals ** 2 / mask_residual_uncertainties ** 2
                ) / (len(mask_residuals) - 4)

            def to_min(x):
                chi2 = chisq(x[0])
                return (chi2 - 1) ** 2

            res_int_disp = minimize(to_min, [intrinsic_dispersion])

            intrinsic_dispersion = res_int_disp.x[0]
            print("  -> new intrinsic_dispersion=%.3f" % intrinsic_dispersion)

        self.salt_MB = MB
        self.salt_alpha = alpha
        self.salt_beta = beta
        self.salt_intrinsic_dispersion = intrinsic_dispersion
        self.salt_wrms = wrms

        residuals, residual_uncertainties = self._evaluate_salt_hubble_residuals(
            MB, alpha, beta, intrinsic_dispersion, pec_vel_disp
        )

        self.salt_hr = residuals
        self.salt_hr_uncertainties = residual_uncertainties

        # Save SALT2 uncertainties without the intrinsic dispersion component.
        self.salt_hr_raw_uncertainties = np.sqrt(
            residual_uncertainties ** 2 - intrinsic_dispersion ** 2
        )

        # Save raw residuals without alpha and beta corrections applied.
        raw_residuals, raw_residual_uncertainties = self._evaluate_salt_hubble_residuals(
            MB, 0, 0, intrinsic_dispersion, pec_vel_disp
        )
        self.salt_hr_raw = raw_residuals - np.mean(raw_residuals)

        print("SALT2 Hubble fit: ")
        print("    MB:   ", self.salt_MB)
        print("    alpha:", self.salt_alpha)
        print("    beta: ", self.salt_beta)
        print("    σ_int:", self.salt_intrinsic_dispersion)
        print("    RMS:  ", np.std(self.salt_hr[self.good_salt_mask]))
        print("    NMAD: ", math.nmad(self.salt_hr[self.good_salt_mask]))
        print("    WRMS: ", self.salt_wrms)

    def scatter(
        self,
        variable,
        mask=None,
        weak_mask=None,
        label="",
        axis_1=0,
        axis_2=1,
        axis_3=None,
        marker_size=40,
        cmap=plt.cm.coolwarm,
        invert_colorbar=False,
        **kwargs
    ):
        """Make a scatter plot of some variable against the Isomap coefficients

        variable is the values to use for the color axis of the plot.

        A boolean array can be specified for cut to specify which points to use in the
        plot. If cut is None, then the full variable list is used.

        The target variable can be passed with or without the cut already applied. This
        function will check and automatically apply it or ignore it so that the variable
        array has the same length as the coefficient arrays.

        Optionally, a weak cut can be performed where spectra not passing the cut are
        plotted as small points rather than being completely omitted. To do this,
        specify the "weak_cut" parameter with a boolean array that has the length of the
        the variable array after the base cut.

        Any kwargs are passed to plt.scatter directly.
        """
        use_embedding = self.embedding
        use_var = variable

        if mask is not None:
            use_embedding = use_embedding[mask]
            use_var = use_var[mask]

        if invert_colorbar:
            cmap = cmap.reversed()

        if weak_mask is None:
            # Constant marker size
            marker_size = marker_size
        else:
            # Variable marker size
            marker_size = 10 + (marker_size - 10) * weak_mask[mask]

        fig = plt.figure()

        if use_embedding.shape[1] >= 3 and axis_3 is not None:
            ax = fig.add_subplot(111, projection="3d")
            plot = ax.scatter(
                use_embedding[:, axis_1],
                use_embedding[:, axis_2],
                use_embedding[:, axis_3],
                s=marker_size,
                c=use_var,
                cmap=cmap,
                **kwargs
            )
            ax.set_zlabel("Component %d" % axis_3)
        else:
            ax = fig.add_subplot(111)
            plot = ax.scatter(
                use_embedding[:, axis_1],
                use_embedding[:, axis_2],
                s=marker_size,
                c=use_var,
                cmap=cmap,
                **kwargs
            )

        ax.set_xlabel("Component %d" % (axis_1 + 1))
        ax.set_ylabel("Component %d" % (axis_2 + 1))

        if label is not None:
            cb = fig.colorbar(plot, label=label)
        else:
            cb = fig.colorbar(plot)

        if invert_colorbar:
            # workaround: in my version of matplotlib, the ticks disappear if
            # you invert the colorbar y-axis. Save the ticks, and put them back
            # to work around that bug.
            ticks = cb.get_ticks()
            cb.ax.invert_yaxis()
            cb.set_ticks(ticks)

        plt.tight_layout()


    def do_component_blondin_plot(self, axis_1=0, axis_2=1, marker_size=40):
        indicators = self.spectral_indicators

        s1 = indicators["EWSiII6355"]
        s2 = indicators["EWSiII5972"]

        plt.figure()

        cut = s2 > 30
        plt.scatter(
            self.embedding[cut, axis_1],
            self.embedding[cut, axis_2],
            s=marker_size,
            c="r",
            label="Cool (CL)",
        )
        cut = (s2 < 30) & (s1 < 70)
        plt.scatter(
            self.embedding[cut, axis_1],
            self.embedding[cut, axis_2],
            s=marker_size,
            c="g",
            label="Shallow silicon (SS)",
        )
        cut = (s2 < 30) & (s1 > 70) & (s1 < 100)
        plt.scatter(
            self.embedding[cut, axis_1],
            self.embedding[cut, axis_2],
            s=marker_size,
            c="black",
            label="Core normal (CN)",
        )
        cut = (s2 < 30) & (s1 > 100)
        plt.scatter(
            self.embedding[cut, axis_1],
            self.embedding[cut, axis_2],
            s=marker_size,
            c="b",
            label="Broad line (BL)",
        )

        plt.xlabel("Component %d" % (axis_1 + 1))
        plt.ylabel("Component %d" % (axis_2 + 1))

        plt.legend()

    def do_blondin_plot(self, marker_size=40):
        indicators = self.spectral_indicators

        s1 = indicators["EWSiII6355"]
        s2 = indicators["EWSiII5972"]

        plt.figure()

        cut = s2 > 30
        plt.scatter(s1[cut], s2[cut], s=marker_size, c="r", label="Cool (CL)")
        cut = (s2 < 30) & (s1 < 70)
        plt.scatter(
            s1[cut], s2[cut], s=marker_size, c="g", label="Shallow silicon (SS)"
        )
        cut = (s2 < 30) & (s1 > 70) & (s1 < 100)
        plt.scatter(
            s1[cut], s2[cut], s=marker_size, c="black", label="Core normal (CN)"
        )
        cut = (s2 < 30) & (s1 > 100)
        plt.scatter(s1[cut], s2[cut], s=marker_size, c="b", label="Broad line (BL)")

        plt.xlabel("SiII 6355 Equivalent Width")
        plt.ylabel("SiII 5972 Equivalent Width")

        plt.legend()

    def do_blondin_plot_3d(self, marker_size=40):
        fig = plt.figure()
        ax = fig.add_subplot(111, projection=Axes3D.name)

        indicators = self.spectral_indicators

        s1 = indicators["EWSiII6355"]
        s2 = indicators["EWSiII5972"]

        embedding = self.embedding

        cut = s2 > 30
        ax.scatter(
            embedding[cut, 0],
            embedding[cut, 1],
            embedding[cut, 2],
            s=marker_size,
            c="r",
            label="Cool (CL)",
        )
        cut = (s2 < 30) & (s1 < 70)
        ax.scatter(
            embedding[cut, 0],
            embedding[cut, 1],
            embedding[cut, 2],
            s=marker_size,
            c="g",
            label="Shallow silicon (SS)",
        )
        cut = (s2 < 30) & (s1 > 70) & (s1 < 100)
        ax.scatter(
            embedding[cut, 0],
            embedding[cut, 1],
            embedding[cut, 2],
            s=marker_size,
            c="black",
            label="Core normal (CN)",
        )
        cut = (s2 < 30) & (s1 > 100)
        ax.scatter(
            embedding[cut, 0],
            embedding[cut, 1],
            embedding[cut, 2],
            s=marker_size,
            c="b",
            label="Broad line (BL)",
        )

        ax.set_xlabel("Component 0")
        ax.set_ylabel("Component 1")
        ax.set_zlabel("Component 2")

        ax.legend()

