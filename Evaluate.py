import numpy as np
import tensorflow as tf
import librosa

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from mpl_toolkits.axes_grid1 import make_axes_locatable

import os
import json
import glob

from Input import Input
import Models.UnetAudioSeparator
import Models.UnetSpectrogramSeparator

import musdb
import museval
import Utils

def predict(track, model_config, load_model, results_dir=None):
    '''
    Function in accordance with MUSB evaluation API. Takes MUSDB track object and computes corresponding source estimates, as well as calls evlauation script.
    Model has to be saved beforehand into a pickle file containing model configuration dictionary and checkpoint path!
    :param track: Track object
    :param results_dir: Directory where SDR etc. values should be saved
    :return: Source estimates dictionary
    '''

    # Determine input and output shapes, if we use U-net as separator
    disc_input_shape = [model_config["batch_size"], model_config["num_frames"], 0]  # Shape of discriminator input
    if model_config["network"] == "unet":
        separator_class = Models.UnetAudioSeparator.UnetAudioSeparator(model_config["num_layers"], model_config["num_initial_filters"],
                                                                   output_type=model_config["output_type"],
                                                                   context=model_config["context"],
                                                                   mono=model_config["mono_downmix"],
                                                                   upsampling=model_config["upsampling"],
                                                                   num_sources=model_config["num_sources"],
                                                                   filter_size=model_config["filter_size"],
                                                                   merge_filter_size=model_config["merge_filter_size"])
    elif model_config["network"] == "unet_spectrogram":
        separator_class = Models.UnetSpectrogramSeparator.UnetSpectrogramSeparator(model_config["num_layers"], model_config["num_initial_filters"],
                                                                       mono=model_config["mono_downmix"],
                                                                       num_sources=model_config["num_sources"])
    else:
        raise NotImplementedError

    sep_input_shape, sep_output_shape = separator_class.get_padding(np.array(disc_input_shape))
    separator_func = separator_class.get_output

    # Batch size of 1
    sep_input_shape[0] = 1
    sep_output_shape[0] = 1

    mix_context, sources = Input.get_multitrack_placeholders(sep_output_shape, model_config["num_sources"], sep_input_shape, "input")

    print("Testing...")

    # BUILD MODELS
    # Separator
    separator_sources = separator_func(mix_context, False, reuse=False)

    # Start session and queue input threads
    sess = tf.Session()
    sess.run(tf.global_variables_initializer())

    # Load model
    # Load pretrained model to continue training, if we are supposed to
    restorer = tf.train.Saver(None, write_version=tf.train.SaverDef.V2)
    print("Num of variables" + str(len(tf.global_variables())))
    restorer.restore(sess, load_model)
    print('Pre-trained model restored for song prediction')

    mix_audio, orig_sr, mix_channels = track.audio, track.rate, track.audio.shape[1] # Audio has (n_samples, n_channels) shape
    separator_preds = predict_track(model_config, sess, mix_audio, orig_sr, sep_input_shape, sep_output_shape, separator_sources, mix_context)

    # Upsample predicted source audio and convert to stereo
    pred_audio = [Utils.resample(pred, model_config["expected_sr"], orig_sr) for pred in separator_preds]

    if model_config["mono_downmix"] and mix_channels > 1: # Convert to multichannel if mixture input was multichannel by duplicating mono estimate
        pred_audio = [np.tile(pred, [1, mix_channels]) for pred in pred_audio]

    # Set estimates depending on estimation task (voice or multi-instrument separation)
    if model_config["task"] == "voice": # [acc, vocals] order
        estimates = {
            'vocals' : pred_audio[1],
            'accompaniment' : pred_audio[0]
        }
    else: # [bass, drums, other, vocals]
        estimates = {
            'bass' : pred_audio[0],
            'drums' : pred_audio[1],
            'other' : pred_audio[2],
            'vocals' : pred_audio[3]
        }

    # Evaluate using museval, if we are currently evaluating MUSDB
    if results_dir is not None:
        scores = museval.eval_mus_track(track, estimates, output_dir=results_dir)

        # print nicely formatted mean scores
        print(scores)

    # Close session, clear computational graph
    sess.close()
    tf.reset_default_graph()

    return estimates

def predict_track(model_config, sess, mix_audio, mix_sr, sep_input_shape, sep_output_shape, separator_sources, mix_context):
    '''
    Outputs source estimates for a given input mixture signal mix_audio [n_frames, n_channels] and a given Tensorflow session and placeholders belonging to the prediction network.
    It iterates through the track, collecting segment-wise predictions to form the output.
    :param model_config: Model configuration dictionary
    :param sess: Tensorflow session used to run the network inference
    :param mix_audio: [n_frames, n_channels] audio signal (numpy array). Can have higher sampling rate or channels than the model supports, will be downsampled correspondingly.
    :param mix_sr: Sampling rate of mix_audio
    :param sep_input_shape: Input shape of separator ([batch_size, num_samples, num_channels])
    :param sep_output_shape: Input shape of separator ([batch_size, num_samples, num_channels])
    :param separator_sources: List of Tensorflow tensors that represent the output of the separator network
    :param mix_context: Input tensor of the network
    :return: 
    '''
    # Load mixture, convert to mono and downsample then
    assert(len(mix_audio.shape) == 2)
    if model_config["mono_downmix"]:
        mix_audio = np.mean(mix_audio, axis=1, keepdims=True)
    else:
        if mix_audio.shape[1] == 1:# Duplicate channels if input is mono but model is stereo
            mix_audio = np.tile(mix_audio, [1, 2])
    mix_audio = Utils.resample(mix_audio, mix_sr, model_config["expected_sr"])

    # Preallocate source predictions (same shape as input mixture)
    source_time_frames = mix_audio.shape[0]
    source_preds = [np.zeros(mix_audio.shape, np.float32) for _ in range(model_config["num_sources"])]

    input_time_frames = sep_input_shape[1]
    output_time_frames = sep_output_shape[1]

    # Pad mixture across time at beginning and end so that neural network can make prediction at the beginning and end of signal
    pad_time_frames = (input_time_frames - output_time_frames) / 2
    mix_audio_padded = np.pad(mix_audio, [(pad_time_frames, pad_time_frames), (0,0)], mode="constant", constant_values=0.0)

    # Iterate over mixture magnitudes, fetch network rpediction
    for source_pos in range(0, source_time_frames, output_time_frames):
        # If this output patch would reach over the end of the source spectrogram, set it so we predict the very end of the output, then stop
        if source_pos + output_time_frames > source_time_frames:
            source_pos = source_time_frames - output_time_frames

        # Prepare mixture excerpt by selecting time interval
        mix_part = mix_audio_padded[source_pos:source_pos + input_time_frames,:]
        mix_part = np.expand_dims(mix_part, axis=0)

        source_parts = sess.run(separator_sources, feed_dict={mix_context: mix_part})

        # Save predictions
        # source_shape = [1, freq_bins, acc_mag_part.shape[2], num_chan]
        for i in range(model_config["num_sources"]):
            source_preds[i][source_pos:source_pos + output_time_frames] = source_parts[i][0, :, :]

    return source_preds

def produce_musdb_source_estimates(model_config, load_model, musdb_path, output_path, subsets=None):
    '''
    Predicts source estimates for MUSDB for a given model checkpoint and configuration, and evaluate them.
    :param model_config: Model configuration of the model to be evaluated
    :param load_model: Model checkpoint path
    :return: 
    '''
    print("Evaluating trained model saved at " + str(load_model)+ " on MUSDB and saving source estimate audio to " + str(output_path))

    mus = musdb.DB(root_dir=musdb_path)
    predict_fun = lambda track : predict(track, model_config, load_model, output_path)
    assert(mus.test(predict_fun))
    mus.run(predict_fun, estimates_dir=output_path, subsets=subsets)

def produce_source_estimates(model_config, load_model, input_path, output_path=None):
    '''
    For a given input mixture file, saves source predictions made by a given model.
    :param model_config: Model configuration
    :param load_model: Model checkpoint path
    :param input_path: Path to input mixture audio file
    :param output_path: Output directory where estimated sources should be saved. Defaults to the same folder as the input file, if not given
    :return: Dictionary of source estimates containing the source signals as numpy arrays
    '''
    print("Producing source estimates for input mixture file " + input_path)
    # Prepare input audio as track object (in the MUSDB sense), so we can use the MUSDB-compatible prediction function
    audio, sr = Utils.load(input_path, sr=None, mono=False)
    # Create something that looks sufficiently like a track object to our MUSDB function
    class TrackLike(object):
        def __init__(self, audio, rate, shape):
            self.audio = audio
            self.rate = rate
            self.shape = shape
    track = TrackLike(audio, sr, audio.shape)

    sources_pred = predict(track, model_config, load_model) # Input track to prediction function, get source estimates

    # Save source estimates as audio files into output dictionary
    input_folder, input_filename = os.path.split(input_path)
    if output_path is None:
        # By default, set it to the input_path folder
        output_path = input_folder
    if not os.path.exists(output_path):
        print("WARNING: Given output path " + output_path + " does not exist. Trying to create it...")
        os.makedirs(output_path)
    assert(os.path.exists(output_path))
    for source_name, source_audio in sources_pred.items():
        librosa.output.write_wav(os.path.join(output_path, input_filename) + "_" + source_name + ".wav", source_audio, sr)

def compute_mean_metrics(json_folder, compute_averages=True):
    files = glob.glob(os.path.join(json_folder, "*.json"))
    sdr_inst_list = None
    for path in files:
        #print(path)
        with open(path, "r") as f:
            js = json.load(f)

        if sdr_inst_list is None:
            sdr_inst_list = [list() for _ in range(len(js["targets"]))]

        for i in range(len(js["targets"])):
            sdr_inst_list[i].extend([np.float(f['metrics']["SDR"]) for f in js["targets"][i]["frames"]])

    #return np.array(sdr_acc), np.array(sdr_voc)
    sdr_inst_list = [np.array(sdr) for sdr in sdr_inst_list]

    if compute_averages:
        return [(np.nanmedian(sdr), np.nanmedian(np.abs(sdr - np.nanmedian(sdr))), np.nanmean(sdr), np.nanstd(sdr)) for sdr in sdr_inst_list]
    else:
        return sdr_inst_list

def draw_violin_sdr(json_folder):
    acc, voc = compute_mean_metrics(json_folder, compute_averages=False)
    acc = acc[~np.isnan(acc)]
    voc = voc[~np.isnan(voc)]
    data = [acc, voc]
    inds = [1,2]

    fig, ax = plt.subplots()
    ax.violinplot(data, showmeans=True, showmedians=False, showextrema=False, vert=False)
    ax.scatter(np.percentile(data, 50, axis=1),inds, marker="o", color="black")
    ax.set_title("Segment-wise SDR distribution")
    ax.vlines([np.min(acc), np.min(voc), np.max(acc), np.max(voc)], [0.8, 1.8, 0.8, 1.8], [1.2, 2.2, 1.2, 2.2], color="blue")
    ax.hlines(inds, [np.min(acc), np.min(voc)], [np.max(acc), np.max(voc)], color='black', linestyle='--', lw=1, alpha=0.5)

    ax.set_yticks([1,2])
    ax.set_yticklabels(["Accompaniment", "Vocals"])

    fig.set_size_inches(8, 3.)
    fig.savefig("sdr_histogram.pdf", bbox_inches='tight')

def draw_spectrogram(example_wav="musb_005_angela thomas wade_audio_model_without_context_cut_28234samples_61002samples_93770samples_126538.wav"):
    y, sr = Utils.load(example_wav, sr=None)
    spec = np.abs(librosa.stft(y, 512, 256, 512))
    norm_spec = librosa.power_to_db(spec**2)
    black_time_frames = np.array([28234, 61002, 93770, 126538]) / 256.0

    fig, ax = plt.subplots()
    img = ax.imshow(norm_spec)
    plt.vlines(black_time_frames, [0, 0, 0, 0], [10, 10, 10, 10], colors="red", lw=2, alpha=0.5)
    plt.vlines(black_time_frames, [256, 256, 256, 256], [246, 246, 246, 246], colors="red", lw=2, alpha=0.5)

    divider = make_axes_locatable(ax)
    cax = divider.append_axes("right", size="5%", pad=0.1)
    plt.colorbar(img, cax=cax)

    ax.xaxis.set_label_position("bottom")
    #ticks_x = ticker.FuncFormatter(lambda x, pos: '{0:g}'.format(x * 256.0 / sr))
    #ax.xaxis.set_major_formatter(ticks_x)
    ax.xaxis.set_major_locator(ticker.FixedLocator(([i * sr / 256. for i in range(len(y)//sr + 1)])))
    ax.xaxis.set_major_formatter(ticker.FixedFormatter(([str(i) for i in range(len(y)//sr + 1)])))

    ax.yaxis.set_major_locator(ticker.FixedLocator(([float(i) * 2000.0 / (sr/2.0) * 256. for i in range(6)])))
    ax.yaxis.set_major_formatter(ticker.FixedFormatter([str(i*2) for i in range(6)]))

    ax.set_xlabel("t (s)")
    ax.set_ylabel('f (KHz)')

    fig.set_size_inches(7., 3.)
    fig.savefig("spectrogram_example.pdf", bbox_inches='tight')
