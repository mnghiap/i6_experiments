from dataclasses import dataclass
import torch
from torch import nn
import multiprocessing
import math
import os
import soundfile

from IPython import embed

from returnn.datasets.hdf import SimpleHDFWriter

from .shared import modules
from .shared import commons
from .shared import attentions
from .monotonic_align import maximum_path

from .shared.feature_extraction import DbMelFeatureExtraction
from .shared.configs import DbMelFeatureExtractionConfig
from .shared.model_config import ModelConfig, FlowDecoderConfig, TextEncoderConfig, ConformerCouplingFlowDecoderConfig

class XVector(nn.Module):
    def __init__(self, input_dim=40, num_classes=8, **kwargs):
        super(XVector, self).__init__()
        self.tdnn1 = modules.TDNN(
            input_dim=input_dim,
            output_dim=512,
            context_size=5,
            dilation=1,
            dropout_p=0.5,
            batch_norm=True
        )
        self.tdnn2 = modules.TDNN(
            input_dim=512, output_dim=512, context_size=3, dilation=2, dropout_p=0.5, batch_norm=True
        )
        self.tdnn3 = modules.TDNN(
            input_dim=512, output_dim=512, context_size=2, dilation=3, dropout_p=0.5, batch_norm=True
        )
        self.tdnn4 = modules.TDNN(
            input_dim=512, output_dim=512, context_size=1, dilation=1, dropout_p=0.5, batch_norm=True
        )
        self.tdnn5 = modules.TDNN(
            input_dim=512, output_dim=512, context_size=1, dilation=1, dropout_p=0.5, batch_norm=True
        )
        #### Frame levelPooling
        self.segment6 = nn.Linear(1024, 512)
        self.segment7 = nn.Linear(512, 512)
        self.output = nn.Linear(512, num_classes)
        self.softmax = nn.Softmax(dim=1)

        # fe_config = DbMelFeatureExtractionConfig.from_dict(kwargs["fe_config"])
        # self.feature_extraction = DbMelFeatureExtraction(config=fe_config)

    def forward(self, x, x_lengths):
        # with torch.no_grad():
        #     squeezed_audio = torch.squeeze(raw_audio)
        #     x, x_lengths = self.feature_extraction(squeezed_audio, raw_audio_lengths)  # [B, T, F]

        # x = x.transpose(1, 2)
        tdnn1_out = self.tdnn1(x)
        # return tdnn1_out
        tdnn2_out = self.tdnn2(tdnn1_out)
        tdnn3_out = self.tdnn3(tdnn2_out)
        tdnn4_out = self.tdnn4(tdnn3_out)
        tdnn5_out = self.tdnn5(tdnn4_out)
        ### Stat Pool
        mean = torch.mean(tdnn5_out, 2)
        std = torch.std(tdnn5_out, 2)
        stat_pooling = torch.cat((mean, std), 1)
        segment6_out = self.segment6(stat_pooling)
        x_vec = self.segment7(segment6_out)
        output = self.output(x_vec)
        predictions = self.softmax(output)
        return output, predictions, x_vec


class DurationPredictor(nn.Module):
    """
    Duration Predictor module, trained using calculated durations coming from monotonic alignment search
    """

    def __init__(self, in_channels, filter_channels, filter_size, p_dropout):
        super().__init__()

        self.in_channels = in_channels
        self.filter_channels = filter_channels
        self.filter_size = filter_size
        self.p_dropout = p_dropout

        self.convs = nn.Sequential(
            modules.Conv1DBlock(
                in_size=self.in_channels,
                out_size=self.filter_channels,
                filter_size=self.filter_size,
                p_dropout=p_dropout,
            ),
            modules.Conv1DBlock(
                in_size=self.filter_channels,
                out_size=self.filter_channels,
                filter_size=self.filter_size,
                p_dropout=p_dropout,
            ),
        )
        self.proj = nn.Conv1d(in_channels=self.filter_channels, out_channels=1, kernel_size=1)

    def forward(self, x, x_mask):
        x_with_mask = (x, x_mask)
        (x, x_mask) = self.convs(x_with_mask)
        x = self.proj(x * x_mask)
        return x


class TextEncoder(nn.Module):
    """
    Text Encoder model
    """

    def __init__(
        self,
        cfg: TextEncoderConfig,
        out_channels,
        gin_channels=0,
    ):
        """Text Encoder Model based on Multi-Head Self-Attention combined with FF-CCNs

        Args:
            
        """
        super().__init__()
        self.cfg = cfg
        self.out_channels = out_channels
        self.gin_channels = gin_channels

        self.emb = nn.Embedding(self.cfg.n_vocab, self.cfg.hidden_channels)
        nn.init.normal_(self.emb.weight, 0.0, self.cfg.hidden_channels**-0.5)

        if self.cfg.prenet:
            self.pre = modules.ConvReluNorm(
                self.cfg.hidden_channels, self.cfg.hidden_channels, self.cfg.hidden_channels, kernel_size=5, n_layers=3, p_dropout=0.5
            )
        self.encoder = attentions.Encoder(
            self.cfg.hidden_channels,
            self.cfg.filter_channels,
            self.cfg.n_heads,
            self.cfg.n_layers,
            self.cfg.kernel_size,
            self.cfg.p_dropout,
            window_size=self.cfg.window_size,
            block_length=self.cfg.block_length,
        )

        self.proj_m = nn.Conv1d(self.cfg.hidden_channels, out_channels, 1)
        if not self.cfg.mean_only:
            self.proj_s = nn.Conv1d(self.cfg.hidden_channels, out_channels, 1)
        self.proj_w = DurationPredictor(self.cfg.hidden_channels + gin_channels, self.cfg.filter_channels_dp, self.cfg.kernel_size, self.cfg.p_dropout)

    def forward(self, x, x_lengths, g=None):
        x = self.emb(x) * math.sqrt(self.cfg.hidden_channels)  # [b, t, h]
        x = torch.transpose(x, 1, -1)  # [b, h, t]
        x_mask = torch.unsqueeze(commons.sequence_mask(x_lengths, x.size(2)), 1).to(x.dtype)

        if self.cfg.prenet:
            x = self.pre(x, x_mask)
        x = self.encoder(x, x_mask)

        if g is not None:
            g_exp = g.unsqueeze(-1).expand(-1, -1, x.size(-1))
            # print(f"Dimension of input in Text Encoder: x.shape: {x.shape}; g: {g.shape}, g_exp: {g_exp.shape}")
            x_dp = torch.cat([torch.detach(x), g_exp], 1)
        else:
            x_dp = torch.detach(x)

        x_m = self.proj_m(x) * x_mask
        if not self.cfg.mean_only:
            x_logs = self.proj_s(x) * x_mask
        else:
            x_logs = torch.zeros_like(x_m)

        # print(f"Dimension of input in Text Encoder before DP: {x_dp.shape}")

        logw = self.proj_w(x_dp, x_mask)
        return x_m, x_logs, logw, x_mask


class FlowDecoder(nn.Module):
    def __init__(
        self,
        cfg: ConformerCouplingFlowDecoderConfig,
        in_channels,
        gin_channels=0,
    ):
        """Flow-based decoder model

        Args:
            cfg (FlowDecoderConfig): Decoder specific parameters wrapped in FlowDecoderConfig
            in_channels (int): Number of incoming channels
        """
        super().__init__()

        self.cfg = cfg
        self.in_channels = in_channels

        self.flows = nn.ModuleList()

        for _ in range(self.cfg.n_blocks):
            self.flows.append(modules.ActNorm(channels=in_channels * self.cfg.n_sqz, ddi=self.cfg.ddi))
            self.flows.append(modules.InvConvNear(channels=in_channels * self.cfg.n_sqz, n_split=self.cfg.n_split))
            self.flows.append(
                attentions.ConformerCouplingBlock(
                    in_channels * self.cfg.n_sqz,
                    self.cfg.hidden_channels,
                    kernel_size=self.cfg.kernel_size,
                    dilation_rate=self.cfg.dilation_rate,
                    n_layers=self.cfg.n_layers,
                    n_heads=self.cfg.n_heads,
                    gin_channels=gin_channels,
                    p_dropout=self.cfg.p_dropout,
                    sigmoid_scale=self.cfg.sigmoid_scale,
                )
            )

    def forward(self, x, x_mask, g=None, reverse=False):
        if not reverse:
            flows = self.flows
            logdet_tot = 0
        else:
            flows = reversed(self.flows)
            logdet_tot = None

        if g is not None:
            g = g.unsqueeze(-1)

        if self.cfg.n_sqz > 1:
            x, x_mask = commons.channel_squeeze(x, x_mask, self.cfg.n_sqz)

        for f in flows:
            if not reverse:
                x, logdet = f(x, x_mask, g=g, reverse=reverse)
                logdet_tot += logdet
            else:
                x, logdet = f(x, x_mask, g=g, reverse=reverse)
        if self.cfg.n_sqz > 1:
            x, x_mask = commons.channel_unsqueeze(x, x_mask, self.cfg.n_sqz)
        return x, logdet_tot

    def store_inverse(self):
        for f in self.flows:
            f.store_inverse()


class Model(nn.Module):
    """
    Flow-based TTS model based on GlowTTS Structure
    Following the definition from https://arxiv.org/abs/2005.11129
    and code from https://github.com/jaywalnut310/glow-tts
    """

    def __init__(
        self,
        model_config: dict,
        **kwargs,
    ):
        """_summary_
        """
        super().__init__()
        self.cfg = ModelConfig.from_dict(model_config)

        fe_config = DbMelFeatureExtractionConfig.from_dict(kwargs["fe_config"])
        self.feature_extraction = DbMelFeatureExtraction(config=fe_config)

        self.encoder = TextEncoder(
            self.cfg.text_encoder_config,
            self.cfg.out_channels,
            gin_channels=self.cfg.gin_channels,
        )

        self.decoder = FlowDecoder(
            self.cfg.decoder_config,
            self.cfg.out_channels,
            gin_channels=self.cfg.gin_channels,
        )

        if self.cfg.n_speakers > 1:
            self.x_vector = XVector(self.cfg.out_channels, self.cfg.n_speakers)

    def forward(
        self, x, x_lengths, raw_audio=None, raw_audio_lengths=None, g=None, gen=False, noise_scale=1.0, length_scale=1.0
    ):
        if not gen:
            with torch.no_grad():
                squeezed_audio = torch.squeeze(raw_audio)
                y, y_lengths = self.feature_extraction(squeezed_audio, raw_audio_lengths)  # [B, T, F]
                y = y.transpose(1, 2)  # [B, F, T]
        else:
            y, y_lengths = (None, None)

        assert (not gen) or (gen and (g is not None)), "Generating speech without given speaker embedding is not supported!"

        if not gen:
            with torch.no_grad():
                _, _, g = self.x_vector(y, y_lengths)

        x_m, x_logs, logw, x_mask = self.encoder(x, x_lengths, g=g)  # mean, std logs, duration logs, mask

        if gen:  # durations from dp only used during generation
            w = torch.exp(logw) * x_mask * length_scale  # durations
            w_ceil = torch.ceil(w)  # durations ceiled
            y_lengths = torch.clamp_min(torch.sum(w_ceil, [1, 2]), 1).long()
            y_max_length = None
        else:
            y_max_length = y.size(2)

        y, y_lengths, y_max_length = self.preprocess(y, y_lengths, y_max_length)
        z_mask = torch.unsqueeze(commons.sequence_mask(y_lengths, y_max_length), 1).to(x_mask.dtype)
        attn_mask = torch.unsqueeze(x_mask, -1) * torch.unsqueeze(z_mask, 2)

        if gen:
            attn = commons.generate_path(w_ceil.squeeze(1), attn_mask.squeeze(1)).unsqueeze(1)
            z_m = torch.matmul(attn.squeeze(1).transpose(1, 2), x_m.transpose(1, 2)).transpose(1, 2)
            z_logs = torch.matmul(attn.squeeze(1).transpose(1, 2), x_logs.transpose(1, 2)).transpose(1, 2)
            logw_ = torch.log(1e-8 + torch.sum(attn, -1)) * x_mask

            z = (z_m + torch.exp(z_logs) * torch.randn_like(z_m) * noise_scale) * z_mask
            y, logdet = self.decoder(z, z_mask, g=g, reverse=True)
            return (y, z_m, z_logs, logdet, z_mask, y_lengths), (x_m, x_logs, x_mask), (attn, logw, logw_)
        else:
            z, logdet = self.decoder(y, z_mask, g=g, reverse=False)
            with torch.no_grad():
                x_s_sq_r = torch.exp(-2 * x_logs)
                logp1 = torch.sum(-0.5 * math.log(2 * math.pi) - x_logs, [1]).unsqueeze(-1)  # [b, t, 1]
                logp2 = torch.matmul(x_s_sq_r.transpose(1, 2), -0.5 * (z**2))  # [b, t, d] x [b, d, t'] = [b, t, t']
                logp3 = torch.matmul((x_m * x_s_sq_r).transpose(1, 2), z)  # [b, t, d] x [b, d, t'] = [b, t, t']
                logp4 = torch.sum(-0.5 * (x_m**2) * x_s_sq_r, [1]).unsqueeze(-1)  # [b, t, 1]
                logp = logp1 + logp2 + logp3 + logp4  # [b, t, t']

                attn = maximum_path(logp, attn_mask.squeeze(1)).unsqueeze(1).detach()
                # embed()

            z_m = torch.matmul(attn.squeeze(1).transpose(1, 2), x_m.transpose(1, 2)).transpose(
                1, 2
            )  # [b, t', t], [b, t, d] -> [b, d, t']
            z_logs = torch.matmul(attn.squeeze(1).transpose(1, 2), x_logs.transpose(1, 2)).transpose(
                1, 2
            )  # [b, t', t], [b, t, d] -> [b, d, t']

            logw_ = torch.log(1e-8 + torch.sum(attn, -1)) * x_mask
            return (z, z_m, z_logs, logdet, z_mask), (x_m, x_logs, x_mask), y_lengths, (attn, logw, logw_)

    def preprocess(self, y, y_lengths, y_max_length):
        if y_max_length is not None:
            y_max_length = (y_max_length // self.cfg.decoder_config.n_sqz) * self.cfg.decoder_config.n_sqz
            y = y[:, :, :y_max_length]
        y_lengths = (y_lengths // self.cfg.decoder_config.n_sqz) * self.cfg.decoder_config.n_sqz
        return y, y_lengths, y_max_length

    def store_inverse(self):
        self.decoder.store_inverse()


def train_step(*, model: Model, data, run_ctx, **kwargs):
    tags = data["seq_tag"]
    audio_features = data["audio_features"]  # [B, T, F]
    # audio_features = audio_features.transpose(1, 2) # [B, F, T] necessary because glowTTS expects the channels to be in the 2nd dimension
    audio_features_len = data["audio_features:size1"]  # [B]

    # perform local length sorting for more efficient packing
    audio_features_len, indices = torch.sort(audio_features_len, descending=True)

    audio_features = audio_features[indices, :, :]
    phonemes = data["phonemes"][indices, :]  # [B, T] (sparse)
    phonemes_len = data["phonemes:size1"][indices]  # [B, T]
    
    tags = list(np.array(tags)[indices.detach().cpu().numpy()])

    (z, z_m, z_logs, logdet, z_mask), (x_m, x_logs, x_mask), y_lengths, (attn, logw, logw_) = model(
        phonemes, phonemes_len, audio_features, audio_features_len 
    )
    # embed()

    l_mle = commons.mle_loss(z, z_m, z_logs, logdet, z_mask)
    l_dp = commons.duration_loss(logw, logw_, phonemes_len)

    run_ctx.mark_as_loss(name="mle", loss=l_mle)
    run_ctx.mark_as_loss(name="dp", loss=l_dp)


############# FORWARD STUFF ################
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra


def forward_init_hook_spectrograms(run_ctx, **kwargs):
    run_ctx.hdf_writer = SimpleHDFWriter("output.hdf", dim=80, ndim=2)
    run_ctx.pool = multiprocessing.Pool(8)


def forward_finish_hook_spectrograms(run_ctx, **kwargs):
    run_ctx.hdf_writer.close()


def forward_init_hook_durations(run_ctx, **kwargs):
    run_ctx.hdf_writer = SimpleHDFWriter("output.hdf", dim=80, ndim=1)
    run_ctx.pool = multiprocessing.Pool(8)


def forward_finish_hook_durations(run_ctx, **kwargs):
    run_ctx.hdf_writer.close()


def forward_init_hook_latent_space(run_ctx, **kwargs):
    run_ctx.hdf_writer_samples = SimpleHDFWriter("samples.hdf", dim=80, ndim=2)
    run_ctx.hdf_writer_mean = SimpleHDFWriter("mean.hdf", dim=80, ndim=2)
    run_ctx.pool = multiprocessing.Pool(8)


def forward_finish_hook_latent_space(run_ctx, **kwargs):
    run_ctx.hdf_writer_samples.close()
    run_ctx.hdf_writer_mean.close()


def forward_init_hook(run_ctx, **kwargs):
    import json
    import utils
    from utils import AttrDict
    from inference import load_checkpoint
    from generator import UnivNet as Generator
    import numpy as np

    with open("/u/lukas.rilling/experiments/glow_tts_asr_v2/config_univ.json") as f:
        data = f.read()

    json_config = json.loads(data)
    h = AttrDict(json_config)

    generator = Generator(h).to(run_ctx.device)

    state_dict_g = load_checkpoint("/work/asr3/rossenbach/rilling/vocoder/univnet/glow_finetuning/g_01080000", run_ctx.device)
    generator.load_state_dict(state_dict_g["generator"])

    run_ctx.generator = generator

    run_ctx.speaker_x_vectors = torch.load(
        "/work/asr3/rossenbach/rilling/sisyphus_work_dirs/glow_tts_asr_v2/i6_core/returnn/forward/ReturnnForwardJob.U6UwGhE7ENbp/output/output_pooled.hdf"
    )

def forward_finish_hook(run_ctx, **kwargs):
    pass


MAX_WAV_VALUE = 32768.0


def forward_step(*, model: Model, data, run_ctx, **kwargs):
    phonemes = data["phonemes"]  # [B, N] (sparse)
    phonemes_len = data["phonemes:size1"]  # [B]
    speaker_labels = data["speaker_labels"]  # [B, 1] (sparse)
    audio_features = data["audio_features"]

    tags = data["seq_tag"]

    speaker_x_vector = run_ctx.speaker_x_vectors[speaker_labels.detach().cpu().numpy(), :].squeeze(1)

    (log_mels, z_m, z_logs, logdet, z_mask, y_lengths), (x_m, x_logs, x_mask), (attn, logw, logw_) = model(
        phonemes,
        phonemes_len,
        g=speaker_x_vector.to(run_ctx.device),
        gen=True,
        noise_scale=kwargs["noise_scale"],
        length_scale=kwargs["length_scale"],
    )

    noise = torch.randn([1, 64, log_mels.shape[-1]]).to(device=log_mels.device)
    audios = run_ctx.generator.forward(noise, log_mels)
    audios = audios * MAX_WAV_VALUE
    audios = audios.cpu().numpy().astype("int16")

    # mels_gt = audio_features.transpose(1, 2)
    # noise = torch.randn([1, 64, mels_gt.shape[-1]]).to(device=mels_gt.device)
    # audios_gt = run_ctx.generator.forward(noise, mels_gt)
    # audios_gt = audios_gt * MAX_WAV_VALUE
    # audios_gt = audios_gt.cpu().numpy().astype("int16")

    if not os.path.exists("/var/tmp/lukas.rilling/"):
        os.makedirs("/var/tmp/lukas.rilling/")
    if not os.path.exists("/var/tmp/lukas.rilling/out"):
        os.makedirs("/var/tmp/lukas.rilling/out/", exist_ok=True)
    for audio, tag in zip(audios, tags):
        soundfile.write(f"/var/tmp/lukas.rilling/out/" + tag.replace("/", "_") + ".wav", audio[0], 16000)
        # soundfile.write(f"/var/tmp/lukas.rilling/out/" + tag.replace("/", "_") + "_gt.wav", audio_gt[0], 16000)


def forward_step_spectrograms(*, model: Model, data, run_ctx, **kwargs):
    tags = data["seq_tag"]
    audio_features = data["audio_features"]  # [B, T, F]
    audio_features = audio_features.transpose(
        1, 2
    )  # [B, F, T] necessary because glowTTS expects the channels to be in the 2nd dimension
    audio_features_len = data["audio_features:size1"]  # [B]

    # perform local length sorting for more efficient packing
    audio_features_len, indices = torch.sort(audio_features_len, descending=True)

    audio_features = audio_features[indices, :, :]
    phonemes = data["phonemes"][indices, :]  # [B, T] (sparse)
    phonemes_len = data["phonemes:size1"][indices]  # [B, T]
    speaker_labels = data["speaker_labels"][indices, :]  # [B, 1] (sparse)
    tags = list(np.array(tags)[indices.detach().cpu().numpy()])

    (y, z_m, z_logs, logdet, z_mask, y_lengths), (x_m, x_logs, x_mask), (attn, logw, logw_) = model(
        phonemes,
        phonemes_len,
        g=speaker_labels,
        gen=True,
        noise_scale=kwargs["noise_scale"],
        length_scale=kwargs["length_scale"],
    )
    spectograms = y.transpose(2, 1).detach().cpu().numpy()  # [B, T, F]

    run_ctx.hdf_writer.insert_batch(spectograms, y_lengths.detach().cpu().numpy(), tags)


def forward_step_durations(*, model: Model, data, run_ctx, **kwargs):
    """Forward Step to output durations in HDF file
    Currently unused due to the name. Only "forward_step" is used in ReturnnForwardJob.
    Rename to use it as the forward step function.

    :param Model model: _description_
    :param _type_ data: _description_
    :param _type_ run_ctx: _description_
    """
    tags = data["seq_tag"]
    audio_features = data["audio_features"]  # [B, T, F]
    audio_features = audio_features.transpose(
        1, 2
    )  # [B, F, T] necessary because glowTTS expects the channels to be in the 2nd dimension
    audio_features_len = data["audio_features:size1"]  # [B]

    # perform local length sorting for more efficient packing
    audio_features_len, indices = torch.sort(audio_features_len, descending=True)

    audio_features = audio_features[indices, :, :]
    phonemes = data["phonemes"][indices, :]  # [B, T] (sparse)
    phonemes_len = data["phonemes:size1"][indices]  # [B, T]
    speaker_labels = data["speaker_labels"][indices, :]  # [B, 1] (sparse)
    tags = list(np.array(tags)[indices.detach().cpu().numpy()])

    # embed()
    (y, z_m, z_logs, logdet, z_mask, y_lengths), (x_m, x_logs, x_mask), (attn, logw, logw_) = model(
        phonemes, phonemes_len, g=speaker_labels, gen=True
    )
    # embed()
    numpy_logprobs = logw.detach().cpu().numpy()

    durations_with_pad = np.round(np.exp(numpy_logprobs) * x_mask.detach().cpu().numpy())
    durations = durations_with_pad.squeeze(1)
    for tag, duration, feat_len, phon_len in zip(tags, durations, audio_features_len, phonemes_len):
        # d = duration[:phon_len]
        # total_sum = np.sum(duration)
        # assert total_sum == feat_len

        # assert len(d) == phon_len
        run_ctx.hdf_writer.insert_batch(np.asarray([duration[:phon_len]]), [phon_len.cpu().numpy()], [tag])


def forward_step_latent_space(*, model: Model, data, run_ctx, **kwargs):
    tags = data["seq_tag"]
    audio_features = data["audio_features"]  # [B, T, F]
    audio_features = audio_features.transpose(
        1, 2
    )  # [B, F, T] necessary because glowTTS expects the channels to be in the 2nd dimension
    audio_features_len = data["audio_features:size1"]  # [B]

    # perform local length sorting for more efficient packing
    audio_features_len, indices = torch.sort(audio_features_len, descending=True)

    audio_features = audio_features[indices, :, :]
    phonemes = data["phonemes"][indices, :]  # [B, T] (sparse)
    phonemes_len = data["phonemes:size1"][indices]  # [B, T]
    speaker_labels = data["speaker_labels"][indices, :]  # [B, 1] (sparse)
    tags = list(np.array(tags)[indices.detach().cpu().numpy()])

    (z, z_m, z_logs, logdet, z_mask), (x_m, x_logs, x_mask), y_lengths, (attn, logw, logw_) = model(
        phonemes, phonemes_len, audio_features, audio_features_len, g=speaker_labels, gen=False
    )
    samples = z.transpose(2, 1).detach().cpu().numpy()  # [B, T, F]
    means = z_m.transpose(2, 1).detach().cpu().numpy()
    run_ctx.hdf_writer_samples.insert_batch(samples, y_lengths.detach().cpu().numpy(), tags)
    run_ctx.hdf_writer_mean.insert_batch(means, y_lengths.detach().cpu().numpy(), tags)
