import h5py
import numpy as np
import pickle as pk
import scipy.io as io
import pandas as pd
import sys
from scipy.interpolate import interp1d

meta_fn_per_dataset_default = [
    '/om/user/rishir/data/pong_basic/RF/occluded_pong_bounce1_pad8_4speed/valid_meta_sample.pkl',
    '/om/group/jazayeri/PongRnn/data/pong_basic/RF/TelePong_H/valid_meta_sample.pkl',
]


def convert_matlab_data_base(fn):
    """
    :param fn: filename of matlab file unpacked by ML_unpack.m
    :return: dictionary of scalar variables value, time, and
    analog/display variables value/time. Note that all time is in microseconds (10^-6).
    """

    def get_matfile_struct_indices(use_ttl=False):
        s_idx = {}
        if use_ttl:
            s_idx['scalar_name_idx'], s_idx['scalar_val_idx'] = 6, 0
            s_idx['ttl_name_idx'], s_idx['ttl_val_idx'] = 7, 1
            s_idx['analog_name_idx'], s_idx['analog_val_idx'] = 8, [2, 3]
            s_idx['display_name_idx'], s_idx['display_val_idx'] = 9, [4, 5]
        else:
            # early behavioral data only had scalar, analog, display fields.
            s_idx['scalar_name_idx'], s_idx['scalar_val_idx'] = 5, 0
            s_idx['analog_name_idx'], s_idx['analog_val_idx'] = 6, [1, 2]
            s_idx['display_name_idx'], s_idx['display_val_idx'] = 7, [3, 4]
        return s_idx

    def get_analog_data_sample(matdat, analog_x_name='paddle_pos_y',
                               analog_sample_name='joystick_output'):
        align_to_strobe = [3, 4]

        strobe_x = matdat['analog_x']['strobe'].astype(float)
        strobe_t = matdat['analog_t']['strobe'].astype(float)
        paddle_y_analog_t = matdat['analog_t'][analog_x_name].astype(float)
        paddle_y_analog_x = matdat['analog_x'][analog_x_name].astype(float)
        trial_idx_all = np.array(matdat['scalar'].index)
        analog_sample = []
        for trial_idx in trial_idx_all:
            try:
                t_end_idx = [x in align_to_strobe for x in strobe_x[trial_idx, :]]
                t_end_idx = np.nonzero(t_end_idx)[0][0]
                t_end = strobe_t[trial_idx, t_end_idx]
                val_idx = np.argmin(np.abs(paddle_y_analog_t[trial_idx, :] - t_end))
                analog_sample.append(paddle_y_analog_x[trial_idx, val_idx])
            except IndexError:
                # likely an ignore trial, where paddle wasn't rendered.
                analog_sample.append(np.nan)

        matdat['scalar'][analog_sample_name] = analog_sample
        return matdat

    def add_samples_from_display(matdat, display_names):
        for dfn in display_names:
            new_fn = '%s_from_mwk' % dfn
            matdat = get_analog_data_sample(matdat, analog_x_name=dfn,
                                            analog_sample_name=new_fn)
        return matdat

    dat = io.loadmat(fn)
    allTrial_data = dat['allTrials'][0, 0].copy()
    ttl_flag = len(allTrial_data) == 10
    ml_s_idx = get_matfile_struct_indices(use_ttl=ttl_flag)

    var_names_scalar = [i[0] for i in allTrial_data[ml_s_idx['scalar_name_idx']][0]]
    data_scalar = pd.DataFrame(allTrial_data[ml_s_idx['scalar_val_idx']], columns=var_names_scalar)

    if ttl_flag:
        var_names_ttl = [i[0] for i in allTrial_data[ml_s_idx['ttl_name_idx']][0]]
        data_ttl = pd.DataFrame(allTrial_data[ml_s_idx['ttl_val_idx']], columns=var_names_ttl)
    else:
        ttl_filler = np.ones((data_scalar.shape[0], 1)) * np.nan
        data_ttl = pd.DataFrame(ttl_filler, columns=['no_ttl'])

    var_names_analog = [i[0] for i in allTrial_data[ml_s_idx['analog_name_idx']][0]]
    var_names_display = [i[0] for i in allTrial_data[ml_s_idx['display_name_idx']][0]]
    data_tv, time_tv = {}, {}
    for i, ai in enumerate(var_names_analog):
        data_tv[ai] = allTrial_data[ml_s_idx['analog_val_idx'][0]][0, i]
        time_tv[ai] = allTrial_data[ml_s_idx['analog_val_idx'][1]][0, i]

    for i, ai in enumerate(var_names_display):
        data_tv[ai] = allTrial_data[ml_s_idx['display_val_idx'][0]][0, i]
        time_tv[ai] = allTrial_data[ml_s_idx['display_val_idx'][1]][0, i]

    data_scalar = data_scalar.loc[:, ~data_scalar.columns.duplicated()]
    data = {
        'scalar': data_scalar,
        'ttl': data_ttl,
        'analog_x': data_tv,
        'analog_t': time_tv,
        'filename': fn}

    data = add_samples_from_display(data, var_names_display)
    return data


class PhysBehavior(object):

    def __init__(self, **kwargs):
        self.if_joystick = kwargs.get('if_joystick', True)
        self.if_eye = kwargs.get('if_eye', False)

        self.display_scale_time = kwargs.get('display_scale_time', 625)
        # use display_scale_time of 15 to sync with RNNs (which have time-steps of 41ms)
        # use display scale_time of 625 for 1ms precision.
        self.tfactor_analytical = 1000000 / (1.6 * self.display_scale_time)
        self.max_trial_time = kwargs.get('max_trial_time', 4000000)
        self.n_analog_samples = round(self.max_trial_time / self.tfactor_analytical)

        self.global_analog_t_reference = kwargs.get('global_analog_t_reference', True)
        # reference time is an extracted sync variable, rather than
        # whatever initial time analog data was collected from
        # (e.g. in case sync is set high before displays are updated with a delay,
        # as is the case for behavior collected while recording physiology).

        self.analog_keys = ['ball_pos_x', 'ball_pos_y',
                            'paddle_pos_x', 'paddle_pos_y',
                            'photodiode']
        if self.if_joystick:
            self.analog_keys += ['joy']
        else:
            self.analog_keys += ['up_pressed', 'down_pressed']
        if self.if_eye:
            self.analog_keys += ['eye_v', 'eye_h']
        self.analog_dimensions = {}

        self.meta_fn_per_dataset = kwargs.get('meta_fn_per_dataset', meta_fn_per_dataset_default)
        self.meta_per_dataset = []
        self.load_meta()

        self.datasets = []
        return

    @staticmethod
    def convert_matlab_data(fn):

        def retrofit_metadata(df_):
            fn_ = fn.split('/')[-1].split('.')[-2]
            fn_tmp = fn.split('/')[-1].split('-')[-2]
            df_['session'] = fn_
            df_['session_date'] = float(fn_tmp)
            df_['success'] = df_['success'].fillna(0)
            df_['failure'] = df_['failure'].fillna(0)
            df_ = df_.loc[:, ~df_.columns.duplicated()]
            return df_

        data = convert_matlab_data_base(fn)
        df = data['scalar'].copy()
        df = retrofit_metadata(df)
        data['scalar'] = df
        return data

    @staticmethod
    def subsample_data(data_in, t):
        data_in['scalar'] = data_in['scalar'][t].reset_index(drop=True)
        data_in['ttl'] = data_in['ttl'][t].reset_index(drop=True)
        analog_keys = ['analog_x', 'analog_t']
        if 'analog_sample' in data_in.keys():
            analog_keys += ['analog_sample']
        for vi in analog_keys:
            for vj in data_in[vi].keys():
                data_in[vi][vj] = data_in[vi][vj][t, :]

        return data_in

    def pool_over_days(self, datasets):
        pooled_data = {'analog_sample': {}}
        for ak in self.analog_keys:
            pooled_data['analog_sample'][ak] = []

        scalar_data, ttl_data = [], []
        for dat in datasets:
            scalar_data.append(dat['scalar'])
            ttl_data.append(dat['ttl'])
            ntr_curr = dat['scalar'].shape[0]
            for ak in self.analog_keys:
                if ak not in dat['analog_sample'].keys():
                    tmp = np.nan * np.ones((ntr_curr, self.n_analog_samples))
                else:
                    tmp = dat['analog_sample'][ak]
                pooled_data['analog_sample'][ak].append(tmp)

        pooled_data['scalar'] = pd.concat(scalar_data, axis=0, ignore_index=True, sort=False)
        pooled_data['ttl'] = pd.concat(ttl_data, axis=0, ignore_index=True, sort=False)
        for ak in self.analog_keys:
            pooled_data['analog_sample'][ak] = np.concatenate(pooled_data['analog_sample'][ak], axis=0)

        return [pooled_data]

    def load_meta(self):
        def load_all_meta(meta_fn):
            meta = pk.load(open(meta_fn, 'rb'))
            save_keys = ['label']
            all_metadata_ = {
                'meta': meta['meta']
            }
            for fk in save_keys:
                all_metadata_[fk] = []

            for batch_idx, batch_fn in enumerate(meta['file_path']):
                tmp = {}
                with h5py.File(batch_fn, 'r') as f:
                    for fk in save_keys:
                        tmp[fk] = f[fk][:]
                for fk in save_keys:
                    if batch_idx == 0:
                        all_metadata_[fk] = tmp[fk]
                    else:
                        all_metadata_[fk] = np.concatenate((all_metadata_[fk], tmp[fk]), axis=0)
            return all_metadata_

        def map_position_to_mworks(x0, rnn_display_size=32.0, mwk_screen_size=20):
            # display_scale_frame = mwk_screen_size / rnn_display_size
            # display_scale_time = 15.0  # convert speed in units/frame_rnn to units/sec, with X frames = 1 sec
            hw = rnn_display_size / 2.0
            return mwk_screen_size / 2.0 * (x0 - hw) / hw

        def augment_with_mworks(meta_sample_, all_metadata_):
            meta_sample_['yf'] = {}
            meta_sample_['x0_mworks'] = {}
            meta_sample_['y0_mworks'] = {}
            meta_sample_['yf_mworks'] = {}

            nsamples = len(meta_sample_['meta_index'])
            for i in range(nsamples):
                idx = meta_sample_['meta_index'][i]
                vidx = np.nonzero(all_metadata_['meta'].index == idx)[0][0]
                meta_sample_['yf'][i] = all_metadata_['label'][vidx, 0]
                meta_sample_['yf_mworks'][i] = map_position_to_mworks(meta_sample_['yf'][i])
                meta_sample_['x0_mworks'][i] = map_position_to_mworks(meta_sample_['x0'][i])
                meta_sample_['y0_mworks'][i] = map_position_to_mworks(meta_sample_['y0'][i])
            return meta_sample_

        for meta_sample_fn in self.meta_fn_per_dataset:
            meta_sample = pk.load(open(meta_sample_fn, 'rb'))
            if not isinstance(meta_sample, dict):
                meta_sample = dict(meta_sample)
            all_metadata = load_all_meta(meta_sample_fn.replace('_meta_sample', ''))
            meta_sample = augment_with_mworks(meta_sample, all_metadata)
            meta_curr = pd.DataFrame(meta_sample)
            self.meta_per_dataset.append(meta_curr)
        return

    def register_with_meta(self, df):
        new_column_fks = []
        for m in self.meta_per_dataset:
            for mk in m.keys():
                if mk not in df.keys():
                    df[mk] = np.nan
                    new_column_fks.append(mk)

        for i in range(df.shape[0]):
            dataset_idx = df['dataset_idx'][i]
            py_meta_idx_curr = df['py_meta_index'][i]
            if np.isnan(dataset_idx):
                continue
            meta_sample = self.meta_per_dataset[int(dataset_idx)]
            tmp2 = meta_sample[meta_sample['meta_index'] == py_meta_idx_curr]
            if tmp2.shape[0] == 1:
                for mk in new_column_fks:
                    if mk in tmp2.keys():
                        df[mk][i] = tmp2[mk].iloc[0]

        if 'paddle_error' not in df.keys():
            df['paddle_y'] = df['joystick_output']
            df['paddle_error'] = np.abs(df['yf_mworks'] - df['paddle_y'])
            df['paddle_error_signed'] = df['paddle_y'] - df['yf_mworks']
        return df

    def get_analog_samples(self, data):
        """ sample mworks analog data at the same sampling rate as RNNs or INTAN.
        """

        def register_single_mworks_time_signal(sig_t, sig_x, t_start_align=None, interp_method='linear'):
            """
            Linear interpolation to get even sampling from mworks events.
            Align to external timestamp or align to start of mworks event stream.
            """
            t_nnan = np.isfinite(sig_x)
            if np.sum(t_nnan) == 0:
                # likely an ignore trial
                return np.ones((self.n_analog_samples,)) * np.nan
            sig_t, sig_x = sig_t[t_nnan], sig_x[t_nnan]
            f1 = interp1d(sig_t, sig_x, kind=interp_method)
            # f1 = interp1d(sig_t, sig_x, kind='nearest')

            if t_start_align is None:
                new_t = np.arange(sig_t[0], np.max(sig_t), self.tfactor_analytical)
                new_t = new_t[new_t < np.nanmax(sig_t)]  # to get around rounding issues from np.arange
                new_x = f1(new_t)
            else:
                new_t = np.arange(t_start_align, np.max(sig_t), self.tfactor_analytical)
                new_t = new_t[new_t < np.nanmax(sig_t)]  # to get around rounding issues from np.arange
                new_x = np.ones(new_t.shape) * np.nan
                t_postpad = new_t >= sig_t[0]
                new_x[t_postpad] = f1(new_t[t_postpad])
                # t_prepad = new_t < sig_t[0]
                # new_x[t_prepad] = np.nan

            if new_x.shape[0] > self.n_analog_samples:
                new_x = new_x[:self.n_analog_samples]
            new_x = np.pad(new_x, (0, self.n_analog_samples - new_x.shape[0]), 'constant',
                           constant_values=(0, np.nan))
            return new_x

        # check_mwork_time_mapping()
        ignore_analog_signals = ['up_pressed', 'down_pressed', 'strobe']  # joy
        analog_signals_to_sample = [x for x in self.analog_keys if x not in ignore_analog_signals]
        reference_time = None
        t_start_align_ = None
        if self.global_analog_t_reference:
            # reference time is an extracted sync variable, rather than
            # whatever initial time analog data was collected from
            # (e.g. in case sync is set high before displays are updated with a delay,
            # as is the case for behavior collected while recording physiology).
            reference_time = np.array(data['ttl']['sync'])

        data['analog_sample'] = {}
        for asig in analog_signals_to_sample:
            if asig not in data['analog_x'].keys():
                print('Failed to sample %s' % asig)
                sys.stdout.flush()
                data['analog_sample'][asig] = []
                continue

            try:
                X, T = data['analog_x'][asig].copy(), data['analog_t'][asig].copy()
                X2 = []
                for ti in range(T.shape[0]):
                    if reference_time is not None:
                        t_start_align_ = reference_time[ti]
                    X2.append(register_single_mworks_time_signal(T[ti, :], X[ti, :], t_start_align_))
                data['analog_sample'][asig] = np.array(X2)
            except():
                print('Failed to sample %s' % asig)
                sys.stdout.flush()
        return data

    @staticmethod
    def clean_eye_data(data):
        ball_x = data['analog_sample']['ball_pos_x']
        eye_x = np.array(data['analog_sample']['eye_h'])
        eye_y = np.array(data['analog_sample']['eye_v'])
        eye_dx = np.diff(eye_x, axis=1, append=np.nan)
        eye_dy = np.diff(eye_y, axis=1, append=np.nan)
        eye_pos_thres, eye_vel_thres = 100, 50

        def test_skip_trial(tr_i):
            no_display = np.nansum(np.isfinite(ball_x[tr_i, :])) == 0
            return no_display

        def get_interpolation_train_test_windows_per_trial(tr_i):
            trial_end_t = np.nanargmax(ball_x[tr_i, :])
            eye_x_curr, eye_y_curr = eye_x[tr_i, :], eye_y[tr_i, :]
            eye_dx_curr, eye_dy_curr = eye_dx[tr_i, :], eye_dy[tr_i, :]

            t_pos = (np.abs(eye_x_curr) >= eye_pos_thres) | (np.abs(eye_y_curr) >= eye_pos_thres)
            t_vel = (np.abs(eye_dx_curr) >= eye_vel_thres) | (np.abs(eye_dy_curr) >= eye_vel_thres)

            idx = np.nonzero(t_pos | t_vel)[0]
            train_win = np.setdiff1d(np.arange(eye_x_curr.shape[0]), idx)
            test_win = idx[idx < trial_end_t]
            return train_win, test_win

        def insert_interpolated_data(x_, train_t, test_t):
            f1 = interp1d(train_t, x_[train_t], kind='linear')
            x_[test_t] = f1(test_t)
            return x_

        def interpolate_per_trial(tr_i):
            eye_x_curr, eye_y_curr = eye_x[tr_i, :], eye_y[tr_i, :]
            train_win, test_win = get_interpolation_train_test_windows_per_trial(tr_i)
            stats = [len(test_win), len(train_win), ]
            eye_x_curr = insert_interpolated_data(eye_x_curr, train_win, test_win)
            eye_y_curr = insert_interpolated_data(eye_y_curr, train_win, test_win)
            return eye_x_curr, eye_y_curr, stats

        stats = []
        nt = eye_x.shape[0]
        for i_ in range(nt):
            if test_skip_trial(i_):
                continue
            eye_x_curr, eye_y_curr, stats_ = interpolate_per_trial(i_)
            # overwrite
            data['analog_sample']['eye_h'][i_, :] = eye_x_curr
            data['analog_sample']['eye_v'][i_, :] = eye_y_curr
            stats.append(stats_)
        stats = np.array(stats)
        prop_samples_fit = np.nanmean(stats[:, 0] / np.nansum(stats, axis=1))
        data['scalar']['proportion_cleaned_eye_positions'] = prop_samples_fit
        return data

    @staticmethod
    def compress_dataset(data):
        def delete_dict_entry(data_, key_):
            try:
                del data_[key_]
            except KeyError:
                print("Key not found")
                sys.stdout.flush()
            return data_

        delete_dict_entry(data, 'analog_x')
        delete_dict_entry(data, 'analog_t')
        return data

    def update_with_data(self, data_fns):
        def update_one(fn):
            new_data = self.convert_matlab_data(fn)
            new_data['scalar'] = self.register_with_meta(new_data['scalar'])
            new_data = self.get_analog_samples(new_data)
            new_data = self.clean_eye_data(new_data)
            new_data = self.compress_dataset(new_data)
            self.datasets.append(new_data)
            self.datasets = self.pool_over_days(self.datasets)
            return

        if isinstance(data_fns, list) is False:
            data_fns = [data_fns]

        for data_fn in data_fns:
            sess_fn = data_fn.split('/')[-1].split('.')[-2]
            print(data_fn, sess_fn)
            sys.stdout.flush()
            if not self.datasets:
                update_one(data_fn)
            elif sess_fn not in list(self.datasets[0]['scalar']['session']):
                update_one(data_fn)
        # self.datasets = self.pool_over_days(self.datasets)
        return
