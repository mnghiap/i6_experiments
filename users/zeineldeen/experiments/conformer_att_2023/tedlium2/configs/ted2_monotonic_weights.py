import copy, os

import numpy

import sisyphus.toolkit as tk

from i6_experiments.users.zeineldeen.experiments.conformer_att_2022.librispeech_960.attention_asr_config import (
    create_config,
    ConformerEncoderArgs,
    TransformerDecoderArgs,
    RNNDecoderArgs,
    ConformerDecoderArgs,
)
from i6_experiments.users.zeineldeen.experiments.conformer_att_2022.librispeech_960.additional_config import (
    apply_fairseq_init_to_conformer,
    reset_params_init,
    apply_fairseq_init_to_transformer_decoder,
)
from i6_experiments.users.zeineldeen.experiments.conformer_att_2023.tedlium2.data import (
    build_training_datasets,
    build_test_dataset,
)
from i6_experiments.users.zeineldeen.experiments.conformer_att_2023.tedlium2.default_tools import (
    RETURNN_ROOT,
    RETURNN_CPU_EXE,
    SCTK_BINARY_PATH,
)
from i6_experiments.users.zeineldeen.experiments.conformer_att_2022.librispeech_960.feature_extraction_net import (
    log10_net_10ms,
)
from i6_experiments.users.zeineldeen.experiments.conformer_att_2022.librispeech_960.pipeline import (
    training,
    search,
    get_average_checkpoint,
    get_best_checkpoint,
    search_single,
)
from i6_experiments.users.zeineldeen.models.lm import generic_lm
from i6_experiments.users.zeineldeen.models.lm.transformer_lm import TransformerLM
from i6_experiments.users.zeineldeen.experiments.conformer_att_2022.librispeech_960 import (
    ilm_helpers,
)
from i6_experiments.users.rossenbach.experiments.librispeech.kazuki_lm.experiment import (
    get_lm,
    ZeineldeenLM,
)

train_jobs_map = {}  # dict[str, ReturnnTrainJob]
train_job_avg_ckpt = {}
train_job_best_epoch = {}

BPE_10K = 10000
BPE_5K = 5000
BPE_1K = 1000
BPE_500 = 500

# train:
# ------
# Seq-length 'data' Stats:
#   92973 seqs
#   Mean: 819.1473868757647
#   Std dev: 434.7168733027807
#   Min/max: 26 / 2049

# --------------------------- LM --------------------------- #

# LM data (runnnig words)
# trans 2250417 ~ 2.25M
# external: 12688261 ~ 12.7M
# Total: 14.9M

lstm_10k_lm_opts = {
    "lm_subnet": generic_lm.libri_lstm_bpe10k_net,
    "lm_model": generic_lm.libri_lstm_bpe10k_model,
    "name": "lstm",
}

lstm_lm_opts_map = {
    BPE_10K: lstm_10k_lm_opts,
}

trafo_lm_net = TransformerLM(source="prev:output", num_layers=24, vocab_size=10025, use_as_ext_lm=True)
trafo_lm_net.create_network()
trafo_10k_lm_opts = {
    "lm_subnet": trafo_lm_net.network.get_net(),
    "load_on_init_opts": {
        "filename": "/work/asr3/irie/experiments/lm/librispeech/2018-03-05--lmbpe-zeyer/data-train/transfo_24_d00.4096_1024.sgd.lr1.8_heads/bk-net-model/network.023",
        "params_prefix": "",
        "load_if_prefix": "lm_output/",
    },
    "name": "trafo",
}

bpe5k_lm = get_lm("ls960_trafo24_bs3000_5ep_5kbpe")  # type: ZeineldeenLM
trafo_5k_lm_opts = {
    "lm_subnet": bpe5k_lm.combination_network,
    "load_on_init_opts": {
        "filename": get_best_checkpoint(bpe5k_lm.train_job, key="dev_score_output/output"),
        "params_prefix": "",
        "load_if_prefix": "lm_output/",
    },
    "name": "trafo",
}

trafo_lm_opts_map = {
    BPE_10K: trafo_10k_lm_opts,
    BPE_5K: trafo_5k_lm_opts,
}

# ----------------------------------------------------------- #


def compute_features_stats(
    output_dirname, feat_dim, bpe_size=10000, feature_extraction_net=log10_net_10ms, model_checkpoint=None, **kwargs
):
    train_data = build_training_datasets(
        bpe_size=bpe_size,
        use_raw_features=True,
        epoch_wise_filter=None,
        link_speed_perturbation=False,
        seq_ordering="laplace:.1000",
        partition_epoch=1,
    )
    # Dump log-mel features into HDFDataset
    dump_features_config = {}
    dump_features_config["extern_data"] = train_data.extern_data
    dump_features_config["network"] = copy.deepcopy(feature_extraction_net)
    if model_checkpoint:
        dump_features_config["network"]["output"] = {
            "class": "hdf_dump",
            "from": "log_mel_features",
            "filename": "log_mel_features.hdf",
        }
    else:
        dump_features_config["network"]["output"] = {
            "class": "copy",
            "from": "log_mel_features",
        }
    dump_features_config["forward_batch_size"] = 20_000 * 80
    dump_features_config["use_tensorflow"] = True
    dump_features_config["eval"] = train_data.train.as_returnn_opts()
    from i6_core.returnn import ReturnnForwardJob, ReturnnConfig

    hdf_filename = "log_mel_features.hdf" if model_checkpoint else "output.hdf"

    dump_features_job = ReturnnForwardJob(
        returnn_config=ReturnnConfig(config=dump_features_config),
        returnn_python_exe=RETURNN_CPU_EXE,
        returnn_root=kwargs.get("returnn_root", RETURNN_ROOT),
        model_checkpoint=model_checkpoint,
        hdf_outputs=[hdf_filename] if model_checkpoint else [],
        device="cpu",
        mem_rqmt=15,
        time_rqmt=72,
        eval_mode=True if model_checkpoint else False,
    )
    dump_features_job.add_alias(f"ted2_stats/{output_dirname}/dump_train_log_mel_features")
    tk.register_output(
        f"ted2_stats/{output_dirname}/log_mel_features.hdf", dump_features_job.out_hdf_files[hdf_filename]
    )

    # Extract features stats from HDFDataset
    extract_stats_returnn_config = ReturnnConfig(
        {
            "extern_data": {
                "data": {"dim": feat_dim},
            },
            "train": {
                "class": "HDFDataset",
                "files": [dump_features_job.out_hdf_files[hdf_filename]],
                "use_cache_manager": True,
            },
            "batch_size": 20_000 * 80,
            "use_tensorflow": True,
        }
    )
    from i6_core.returnn.dataset import ExtractDatasetMeanStddevJob

    extract_mean_stddev_job = ExtractDatasetMeanStddevJob(
        returnn_config=extract_stats_returnn_config,
        returnn_python_exe=RETURNN_CPU_EXE,
        returnn_root=kwargs.get("returnn_root", RETURNN_ROOT),
    )
    extract_mean_stddev_job.add_alias(f"ted2_stats/{output_dirname}/extract_mean_stddev")

    tk.register_output(f"ted2_stats/{output_dirname}/mean_var", extract_mean_stddev_job.out_mean)
    tk.register_output(f"ted2_stats/{output_dirname}/std_dev_var", extract_mean_stddev_job.out_std_dev)
    tk.register_output(f"ted2_stats/{output_dirname}/mean_file", extract_mean_stddev_job.out_mean_file)
    tk.register_output(f"ted2_stats/{output_dirname}/std_dev_file", extract_mean_stddev_job.out_std_dev_file)

    return (
        extract_mean_stddev_job.out_mean,
        extract_mean_stddev_job.out_std_dev,
        extract_mean_stddev_job.out_mean_file,
        extract_mean_stddev_job.out_std_dev_file,
    )


def conformer_baseline():
    abs_name = os.path.abspath(__file__)
    prefix_name = os.path.basename(abs_name)[: -len(".py")]

    def get_test_dataset_tuples(bpe_size):
        test_dataset_tuples = {}
        for testset in ["dev", "test"]:
            test_dataset_tuples[testset] = build_test_dataset(
                testset,
                use_raw_features=True,
                bpe_size=bpe_size,
            )
        return test_dataset_tuples

    def run_train(
        exp_name,
        train_args,
        train_data,
        feature_extraction_net,
        num_epochs,
        recog_epochs,
        **kwargs,
    ):
        exp_prefix = os.path.join(prefix_name, exp_name)
        returnn_config = create_config(
            training_datasets=train_data,
            **train_args,
            feature_extraction_net=feature_extraction_net,
            recog_epochs=recog_epochs,
        )
        train_job = training(
            exp_prefix,
            returnn_config,
            RETURNN_CPU_EXE,
            RETURNN_ROOT,
            num_epochs=num_epochs,
            gpu_mem=kwargs.get("gpu_mem", 11),
        )
        return train_job

    def run_single_search(
        exp_name,
        train_data,
        search_args,
        checkpoint,
        feature_extraction_net,
        recog_dataset,
        recog_ref,
        recog_bliss,
        mem_rqmt=8,
        time_rqmt=4,
        **kwargs,
    ):
        exp_prefix = os.path.join(prefix_name, exp_name)
        returnn_search_config = create_config(
            training_datasets=train_data,
            **search_args,
            feature_extraction_net=feature_extraction_net,
            is_recog=True,
        )
        search_single(
            exp_prefix,
            returnn_search_config,
            checkpoint,
            recognition_dataset=recog_dataset,
            recognition_reference=recog_ref,
            recognition_bliss_corpus=recog_bliss,
            returnn_exe=RETURNN_CPU_EXE,
            returnn_root=RETURNN_ROOT,
            mem_rqmt=mem_rqmt,
            time_rqmt=time_rqmt,
            use_sclite=True,
        )

    def run_lm_fusion(
        lm_type,
        exp_name,
        epoch,
        test_set_names,
        lm_scales,
        train_job,
        train_data,
        feature_net,
        bpe_size,
        args,
        beam_size=12,
        prior_scales=None,
        prior_type=None,
        mini_lstm_ckpt=None,
        length_norm=True,
        prior_type_name=None,
        coverage_scale=None,
        coverage_threshold=None,
        **kwargs,
    ):
        assert lm_type in ["lstm", "trafo"], "lm type should be lstm or trafo"

        if isinstance(lm_scales, float):
            lm_scales = [lm_scales]
        if prior_scales and isinstance(prior_scales, float):
            prior_scales = [prior_scales]
        if isinstance(test_set_names, str):
            test_set_names = [test_set_names]
        assert isinstance(test_set_names, list)

        if epoch == "avg":
            search_checkpoint = train_job_avg_ckpt[exp_name]
        elif epoch == "best":
            search_checkpoint = train_job_best_epoch[exp_name]
        else:
            assert isinstance(epoch, int), "epoch must be either a defined integer or a string in {avg, best}."
            search_checkpoint = train_job.out_checkpoints[epoch]

        ext_lm_opts = lstm_lm_opts_map[bpe_size] if lm_type == "lstm" else trafo_lm_opts_map[bpe_size]

        time_rqmt = 1.0

        search_args = copy.deepcopy(args)

        if lm_type == "lstm":
            if beam_size > 128:
                search_args["batch_size"] = 4000 * 160

        if lm_type == "trafo":
            search_args["batch_size"] = 4000 * 160 if beam_size <= 32 else 2000 * 160
            time_rqmt = 2
            if beam_size > 50:
                time_rqmt = 3

        search_args["beam_size"] = beam_size
        if kwargs.get("batch_size", None):
            search_args["batch_size"] = kwargs["batch_size"]

        if not length_norm:
            search_args["decoder_args"].length_normalization = False

        if "decoder_args" in kwargs:
            for k, v in kwargs["decoder_args"].items():
                setattr(search_args["decoder_args"], k, v)

        scales = [(e,) for e in lm_scales]

        for test_set in test_set_names:
            if prior_scales:
                import itertools

                scales = itertools.product(lm_scales, prior_scales)

            for scale in scales:
                lm_scale = scale[0]
                prior_scale = scale[1] if len(scale) == 2 else None
                if prior_scale and prior_scale > lm_scale:
                    continue

                # External LM opts
                ext_lm_opts["lm_scale"] = lm_scale
                search_args["ext_lm_opts"] = ext_lm_opts

                # ILM opts
                if prior_scale:
                    ilm_opts = {
                        "scale": prior_scale,
                        "type": prior_type,
                        "ctx_dim": search_args["encoder_args"].enc_key_dim,  # this is needed for mini-lstm
                    }
                    # this is needed for mini-self-att
                    if hasattr(search_args["decoder_args"], "num_layers"):
                        ilm_opts["num_dec_layers"] = search_args["decoder_args"].num_layers
                        search_args["decoder_args"].create_ilm_decoder = True
                        search_args["decoder_args"].ilm_type = prior_type

                    ilm_opts.update(kwargs.get("ilm_train_opts", {}))  # example for FFN, etc

                    search_args["prior_lm_opts"] = ilm_opts
                    search_args["preload_from_files"] = {
                        "prior_lm": {
                            "filename": search_checkpoint,  # copy ASR decoder to be used as ILM decoder
                            "prefix": "prior_",
                        }
                    }
                    if prior_type == "mini_lstm" or prior_type == "ffn":
                        assert mini_lstm_ckpt, "Mini-LSTM checkpoint not set."
                        search_args["preload_from_files"].update(
                            {
                                "mini_lstm": {
                                    "filename": mini_lstm_ckpt,
                                    "prefix": "mini_",
                                }
                            }
                        )

                if prior_type_name is None:
                    prior_type_name = prior_type

                lm_desc = f"lm-scale-{lm_scale}"
                if prior_scale:
                    lm_desc += f"-prior-{prior_scale}-{prior_type_name}"
                lm_desc += f"-beam-{beam_size}"
                if length_norm is False:
                    lm_desc += "-woLenNorm"

                if coverage_scale and coverage_threshold:
                    assert isinstance(search_args["decoder_args"], RNNDecoderArgs)
                    search_args["decoder_args"].coverage_scale = coverage_scale
                    search_args["decoder_args"].coverage_threshold = coverage_threshold
                    lm_desc += f"_coverage-thre{coverage_threshold}-scale{coverage_scale}"

                name = f"{exp_name}/recog-{lm_type}-lm/ep-{epoch}/{lm_desc}/{test_set}"

                test_dataset_tuples = get_test_dataset_tuples(bpe_size=bpe_size)

                run_single_search(
                    exp_name=name,
                    train_data=train_data,
                    search_args=search_args,
                    checkpoint=search_checkpoint,
                    feature_extraction_net=feature_net,
                    recog_dataset=test_dataset_tuples[test_set][0],
                    recog_ref=test_dataset_tuples[test_set][1],
                    recog_bliss=test_dataset_tuples[test_set][2],
                    time_rqmt=kwargs.get("time_rqmt", time_rqmt),
                )

    def run_search(
        exp_name,
        train_args,
        train_data,
        train_job,
        feature_extraction_net,
        num_epochs,
        search_args,
        recog_epochs,
        bpe_size,
        **kwargs,
    ):
        exp_prefix = os.path.join(prefix_name, exp_name)

        search_args = search_args if search_args is not None else train_args

        returnn_search_config = create_config(
            training_datasets=train_data,
            **search_args,
            feature_extraction_net=feature_extraction_net,
            is_recog=True,
        )

        num_avg = kwargs.get("num_avg", 4)
        averaged_checkpoint = get_average_checkpoint(
            train_job,
            returnn_exe=RETURNN_CPU_EXE,
            returnn_root=RETURNN_ROOT,
            num_average=num_avg,
            key=kwargs.get("avg_key", "dev_score_output/output_prob"),
        )
        if num_avg == 4:  # TODO: just for now to not break hashes
            train_job_avg_ckpt[exp_name] = averaged_checkpoint

        best_checkpoint = get_best_checkpoint(train_job, key=kwargs.get("avg_key", "dev_score_output/output_prob"))
        train_job_best_epoch[exp_name] = best_checkpoint

        if recog_epochs is None:
            default_recog_epochs = [40]
            default_recog_epochs += [80 * i for i in range(1, int(num_epochs / 80) + 1)]
            if num_epochs % 80 != 0:
                default_recog_epochs += [num_epochs]
        else:
            default_recog_epochs = recog_epochs

        test_dataset_tuples = get_test_dataset_tuples(bpe_size=bpe_size)

        run_only_avg = kwargs.get("run_only_avg", False)

        if not run_only_avg:
            for ep in default_recog_epochs:
                search(
                    exp_prefix + f"/recogs/ep-{ep}",
                    returnn_search_config,
                    train_job.out_checkpoints[ep],
                    test_dataset_tuples,
                    RETURNN_CPU_EXE,
                    RETURNN_ROOT,
                )

            search(
                exp_prefix + "/default_last",
                returnn_search_config,
                train_job.out_checkpoints[num_epochs],
                test_dataset_tuples,
                RETURNN_CPU_EXE,
                RETURNN_ROOT,
            )

            search(
                exp_prefix + "/default_best",
                returnn_search_config,
                best_checkpoint,
                test_dataset_tuples,
                RETURNN_CPU_EXE,
                RETURNN_ROOT,
                use_sclite=True,
            )

        beam_size = search_args.get("beam_size", 12)
        if beam_size != 12:
            exp_prefix += f"_beam-{beam_size}"
        if search_args["decoder_args"].coverage_scale:
            exp_prefix += f"_coverage-thre{search_args['decoder_args'].coverage_threshold}-scale{search_args['decoder_args'].coverage_scale}"
        search(
            exp_prefix + f"/average_{num_avg}",
            returnn_search_config,
            averaged_checkpoint,
            test_dataset_tuples,
            RETURNN_CPU_EXE,
            RETURNN_ROOT,
            use_sclite=True,
        )

    def run_concat_seq_recog(exp_name, corpus_names, num, train_data, search_args, checkpoint, mem_rqmt=8, time_rqmt=1):
        exp_prefix = os.path.join(prefix_name, exp_name)

        from i6_experiments.users.zeineldeen.experiments.chunkwise_att_2023.concat_seqs import (
            ConcatDatasetSeqsJob,
            ConcatSeqsDataset,
            CreateConcatSeqsCTMAndSTMJob,
        )
        from i6_core.corpus.convert import CorpusToStmJob

        if isinstance(corpus_names, str):
            corpus_names = [corpus_names]
        assert isinstance(corpus_names, list)

        for corpus_name in corpus_names:
            test_datasets = get_test_dataset_tuples(bpe_size=BPE_1K)
            stm = CorpusToStmJob(bliss_corpus=test_datasets[corpus_name][2]).out_stm_path
            tk.register_output(f"concat_seqs/{num}/orig_{corpus_name}_stm", stm)
            concat_dataset_seqs = ConcatDatasetSeqsJob(
                corpus_name="TED-LIUM-realease2", stm=stm, num=num, overlap_dur=None
            )
            tk.register_output(f"concat_seqs/{num}/{corpus_name}_stm", concat_dataset_seqs.out_stm)
            tk.register_output(f"concat_seqs/{num}/{corpus_name}_tags", concat_dataset_seqs.out_concat_seq_tags)
            tk.register_output(f"concat_seqs/{num}/{corpus_name}_lens", concat_dataset_seqs.out_concat_seq_lens_py)

            returnn_search_config = create_config(
                training_datasets=train_data,
                **search_args,
                feature_extraction_net=log10_net_10ms,
                is_recog=True,
            )

            returnn_concat_dataset = ConcatSeqsDataset(
                dataset=test_datasets[corpus_name][0].as_returnn_opts(),
                seq_tags=concat_dataset_seqs.out_concat_seq_tags,
                seq_lens_py=concat_dataset_seqs.out_orig_seq_lens_py,
            )

            _, search_words = search_single(
                os.path.join(exp_prefix, corpus_name),
                returnn_search_config,
                checkpoint,
                recognition_dataset=returnn_concat_dataset,
                recognition_reference=test_datasets[corpus_name][1],
                recognition_bliss_corpus=test_datasets[corpus_name][2],
                returnn_exe=RETURNN_CPU_EXE,
                returnn_root=RETURNN_ROOT,
                mem_rqmt=mem_rqmt,
                time_rqmt=time_rqmt,
                # no scoring
                use_sclite=False,
                use_returnn_compute_wer=False,
            )

            from i6_core.corpus.convert import CorpusToStmJob
            from i6_core.recognition.scoring import ScliteJob

            stm_file = concat_dataset_seqs.out_stm

            concat_ctm_and_stm_job = CreateConcatSeqsCTMAndSTMJob(
                recog_words_file=search_words, stm_py_file=concat_dataset_seqs.out_stm_py, stm_file=stm_file
            )
            tk.register_output(exp_prefix + f"/{corpus_name}/sclite/stm", concat_ctm_and_stm_job.out_stm_file)
            tk.register_output(exp_prefix + f"/{corpus_name}/sclite/ctm", concat_ctm_and_stm_job.out_ctm_file)

            sclite_job = ScliteJob(
                ref=concat_ctm_and_stm_job.out_stm_file,
                hyp=concat_ctm_and_stm_job.out_ctm_file,
                sctk_binary_path=SCTK_BINARY_PATH,
            )
            tk.register_output(exp_prefix + f"/{corpus_name}/sclite/wer", sclite_job.out_wer)
            tk.register_output(exp_prefix + f"/{corpus_name}/sclite/report", sclite_job.out_report_dir)

    def run_exp(
        exp_name,
        train_args,
        feature_extraction_net=log10_net_10ms,
        num_epochs=300,
        search_args=None,
        recog_epochs=None,
        bpe_size=1000,
        partition_epoch=4,
        **kwargs,
    ):
        if train_args.get("retrain_checkpoint", None):
            assert kwargs.get("epoch_wise_filter", None) is None, "epoch_wise_filter should be disabled for retraining."
        train_data = build_training_datasets(
            bpe_size=bpe_size,
            use_raw_features=True,
            epoch_wise_filter=kwargs.get("epoch_wise_filter", [(1, 5, 1000)]),
            link_speed_perturbation=train_args.get("speed_pert", True),
            seq_ordering=kwargs.get("seq_ordering", "laplace:.1000"),
            partition_epoch=partition_epoch,
            devtrain_subset=kwargs.get("devtrain_subset", 507),  # same as num of dev segments
        )
        train_job = run_train(
            exp_name,
            train_args,
            train_data,
            feature_extraction_net,
            num_epochs,
            recog_epochs,
            **kwargs,
        )
        train_jobs_map[exp_name] = train_job

        run_search(
            exp_name,
            train_args,
            train_data,
            train_job,
            feature_extraction_net,
            num_epochs,
            search_args,
            recog_epochs,
            bpe_size=bpe_size,
            **kwargs,
        )

        if kwargs.get("concat_recog_opts", None):
            ckpt_ = kwargs["concat_recog_opts"]["checkpoint"]
            if isinstance(ckpt_, str):
                assert ckpt_ in ["best", "avg"]
                if ckpt_ == "best":
                    concat_recog_ckpt = train_job_best_epoch[exp_name]
                else:
                    concat_recog_ckpt = train_job_avg_ckpt[exp_name]
            elif isinstance(ckpt_, int):
                concat_recog_ckpt = train_job.out_checkpoints[ckpt_]
            else:
                raise TypeError(f"concat_recog_opts['checkpoint'] must be str or int, got {type(ckpt_)}")
            concat_recog_search_args = kwargs["concat_recog_opts"].get("search_args", None)
            search_args_ = copy.deepcopy(train_args)
            if concat_recog_search_args:
                search_args_.update(concat_recog_search_args)
            run_concat_seq_recog(
                exp_name=exp_name + f"_concat{kwargs['concat_recog_opts']['num']}",
                corpus_names=kwargs["concat_recog_opts"]["corpus_names"],
                num=kwargs["concat_recog_opts"]["num"],
                train_data=train_data,
                search_args=search_args_,
                checkpoint=concat_recog_ckpt,
            )

        return train_job, train_data

    def train_mini_lstm(
        exp_name,
        checkpoint,
        args,
        num_epochs=20,
        lr=8e-4,
        time_rqmt=4,
        l2=1e-4,
        name="mini_lstm",
        w_drop=False,
        use_dec_state=False,
        use_ffn=False,
        ffn_opts=None,
        **kwargs,
    ):
        if not w_drop:
            params_freeze_str = ilm_helpers.get_mini_lstm_params_freeze_str()
        else:
            if use_ffn:
                params_freeze_str = ilm_helpers.get_ffn_params_freeze_str_w_drop(ffn_opts["num_ffn_layers"])
            else:
                params_freeze_str = ilm_helpers.get_mini_lstm_params_freeze_str_w_drop()

        mini_lstm_args = copy.deepcopy(args)
        mini_lstm_args["batch_size"] = 20000 * 160
        mini_lstm_args["with_pretrain"] = False
        mini_lstm_args["lr"] = lr
        mini_lstm_args["allow_lr_scheduling"] = False
        mini_lstm_args["encoder_args"].with_ctc = False
        mini_lstm_args["keep_all_epochs"] = True  # keep everything
        mini_lstm_args["extra_str"] = params_freeze_str
        mini_lstm_args["preload_from_files"] = {
            "import": {
                "init_for_train": True,
                "ignore_missing": True,
                "filename": checkpoint,
            }
        }
        mini_lstm_args.update(kwargs)

        exp_prefix = os.path.join(prefix_name, exp_name, name)
        mini_lstm_train_data = build_training_datasets(
            bpe_size=10000,
            use_raw_features=True,
            epoch_wise_filter=None,
            link_speed_perturbation=False,  # depends only on text
            seq_ordering=kwargs.get("seq_ordering", "laplace:.1000"),
        )
        returnn_config = create_config(
            training_datasets=mini_lstm_train_data,
            **mini_lstm_args,
            feature_extraction_net=log10_net_10ms,
        )

        inp = "s" if use_dec_state else "prev:target_embed"

        if use_ffn:
            x = inp
            activations = ffn_opts["activations"]
            for l in range(ffn_opts["num_ffn_layers"]):
                returnn_config.config["network"]["output"]["unit"]["ffn_%02i" % (l + 1)] = {
                    "class": "linear",
                    "n_out": ffn_opts["ffn_dims"][l],
                    "L2": l2,
                    "from": inp,
                    "activation": activations[l] if activations and l < len(activations) else None,
                }
                x = "ffn_%02i" % (l + 1)

            returnn_config.config["network"]["output"]["unit"]["att"] = {
                "class": "linear",
                "from": x,
                "activation": None,
                "n_out": mini_lstm_args["encoder_args"].enc_key_dim,
                "L2": l2,
            }
        else:
            # Mini-LSTM + FF

            returnn_config.config["network"]["output"]["unit"]["att_lstm"] = {
                "class": "rec",
                "unit": "nativelstm2",
                "from": inp,
                "n_out": 50,
            }

            returnn_config.config["network"]["output"]["unit"]["att"] = {
                "class": "linear",
                "from": "att_lstm",
                "activation": None,
                "n_out": mini_lstm_args["encoder_args"].enc_key_dim,
                "L2": l2,
            }

        train_job = training(
            exp_prefix,
            returnn_config,
            RETURNN_CPU_EXE,
            RETURNN_ROOT,
            num_epochs=num_epochs,
            time_rqmt=time_rqmt,
        )
        return train_job

    def train_mini_self_att(
        exp_name,
        checkpoint,
        args,
        num_epochs=20,
        lr=8e-4,
        time_rqmt=4,
        name="mini_self_att",
        **kwargs,
    ):
        """
        Same idea as Mini-LSTM but use masked (mini-)self-attention models instead of cross attention.
        Note that each layer has its own (mini-)self-attention.

        In the case of transformer decoder, we want to replace cross-attention layers namely:
            transformer_decoder_{idx}_att_linear
        with masked self-attention models.
        """

        params_freeze_str = ilm_helpers.get_mini_self_att_params_freeze_str_w_drop(args["decoder_args"].num_layers)

        mini_self_att = copy.deepcopy(args)
        mini_self_att["batch_size"] = 20000 * 160  # TODO: does this fit now?
        mini_self_att["with_pretrain"] = False
        mini_self_att["lr"] = lr
        mini_self_att["allow_lr_scheduling"] = False
        mini_self_att["encoder_args"].with_ctc = False
        # mini_self_att['keep_all_epochs'] = True  # keep everything
        mini_self_att["extra_str"] = params_freeze_str
        mini_self_att["preload_from_files"] = {
            "import": {
                "init_for_train": True,
                "ignore_missing": True,
                "filename": checkpoint,
            }
        }
        if "decoder_args" in kwargs:
            assert isinstance(kwargs["decoder_args"], dict)
            for k, v in kwargs["decoder_args"].items():
                setattr(mini_self_att["decoder_args"], k, v)
            kwargs.pop("decoder_args")
        mini_self_att.update(kwargs)

        exp_prefix = os.path.join(prefix_name, exp_name, name)
        mini_self_att_train_data = build_training_datasets(
            bpe_size=10000,
            use_raw_features=True,
            epoch_wise_filter=None,
            link_speed_perturbation=False,  # depends only on text
            seq_ordering=kwargs.get("seq_ordering", "laplace:.1000"),
        )

        # use masked self-att instead of cross-att with layer names having "ilm_" as prefix
        mini_self_att["decoder_args"].replace_cross_att_w_masked_self_att = True

        returnn_config = create_config(
            training_datasets=mini_self_att_train_data,
            **mini_self_att,
            feature_extraction_net=log10_net_10ms,
        )
        train_job = training(
            exp_prefix,
            returnn_config,
            RETURNN_CPU_EXE,
            RETURNN_ROOT,
            num_epochs=num_epochs,
            time_rqmt=time_rqmt,
        )
        return train_job

    # --------------------------- General Settings --------------------------- #

    conformer_enc_args = ConformerEncoderArgs(
        num_blocks=12,
        input_layer="conv-6",
        att_num_heads=8,
        ff_dim=2048,
        enc_key_dim=512,
        conv_kernel_size=32,
        pos_enc="rel",
        dropout=0.1,
        att_dropout=0.1,
        l2=0.0001,
    )
    apply_fairseq_init_to_conformer(conformer_enc_args)
    conformer_enc_args.ctc_loss_scale = 1.0

    rnn_dec_args = RNNDecoderArgs()

    trafo_dec_args = TransformerDecoderArgs(
        num_layers=6,
        embed_dropout=0.1,
        label_smoothing=0.1,
        apply_embed_weight=True,
        pos_enc="rel",
    )
    apply_fairseq_init_to_transformer_decoder(trafo_dec_args)

    conformer_dec_args = ConformerDecoderArgs()
    apply_fairseq_init_to_conformer(conformer_dec_args)

    training_args = dict()

    # LR scheduling
    training_args["const_lr"] = [42, 100]  # use const LR during pretraining
    training_args["wup_start_lr"] = 0.0002
    training_args["wup"] = 20
    training_args["with_staged_network"] = True
    training_args["speed_pert"] = True

    trafo_training_args = copy.deepcopy(training_args)
    trafo_training_args["pretrain_opts"] = {
        "variant": 3,
        "initial_batch_size": 20000 * 160,
    }
    trafo_training_args["pretrain_reps"] = 5
    trafo_training_args["batch_size"] = 12000 * 160  # frames * samples per frame

    trafo_dec_exp_args = copy.deepcopy(
        {
            **trafo_training_args,
            "encoder_args": conformer_enc_args,
            "decoder_args": trafo_dec_args,
        }
    )

    conformer_dec_exp_args = copy.deepcopy(trafo_dec_exp_args)
    conformer_dec_exp_args["decoder_args"] = conformer_dec_args

    lstm_training_args = copy.deepcopy(training_args)
    lstm_training_args["pretrain_opts"] = {
        "variant": 3,
        "initial_batch_size": 22500 * 160,
    }
    lstm_training_args["pretrain_reps"] = 5
    lstm_training_args["batch_size"] = 15000 * 160  # frames * samples per frame

    lstm_dec_exp_args = copy.deepcopy(
        {
            **lstm_training_args,
            "encoder_args": conformer_enc_args,
            "decoder_args": rnn_dec_args,
        }
    )

    # --------------------------- Experiments --------------------------- #

    oclr_args = copy.deepcopy(lstm_dec_exp_args)
    oclr_args["oclr_opts"] = {
        "peak_lr": 9e-4,
        "final_lr": 1e-6,
    }
    oclr_args["encoder_args"].input_layer = "conv-6"
    oclr_args["encoder_args"].use_sqrd_relu = True
    oclr_args["max_seq_length"] = None

    _, _, global_mean, global_std = compute_features_stats(output_dirname="logmel_80", feat_dim=80)

    # step-based: 8.5/8.2
    # epoch-based: 8.6/8.2
    # for bpe_size in [BPE_1K]:
    #     for ep in [50 * 4]:
    #         for lr in [8e-4]:
    #             args = copy.deepcopy(oclr_args)
    #             args["oclr_opts"]["total_ep"] = ep
    #             args["oclr_opts"]["cycle_ep"] = int(0.45 * ep)
    #             args["oclr_opts"]["n_step"] = 1480
    #             args["oclr_opts"]["peak_lr"] = lr
    #             exp_name = f"base_bpe{bpe_size}_peakLR{lr}_ep{ep}"
    #             run_exp(
    #                 exp_name,
    #                 args,
    #                 num_epochs=ep,
    #                 epoch_wise_filter=None,
    #                 bpe_size=bpe_size,
    #                 partition_epoch=4,
    #                 devtrain_subset=3000,
    #             )

    # --------------------- V1 ---------------------
    def get_base_v1_args(lr, ep, enc_drop=0.1, pretrain_reps=3, use_legacy_stats=True):
        #  base_bpe1000_peakLR0.0008_ep200_globalNorm_epochOCLR_pre3_fixZoneout_encDrop0.1_woDepthConvPre
        # Average ckpt: 8.19/7.64 (50 epochs)
        # - Epoch-based OCLR with peak LR 8e-4
        # - EncDrop 0.1, fixed zoneout
        # - Pretrain 3, no depthwise conv pretrain
        # - Feature global normalization

        base_v1_args = copy.deepcopy(oclr_args)
        base_v1_args.pop("oclr_opts")
        cyc_ep = int(0.45 * ep)
        # Epoch-based OCLR
        base_v1_args["learning_rates_list"] = (
            list(numpy.linspace(lr / 10, lr, cyc_ep))
            + list(numpy.linspace(lr, lr / 10, cyc_ep))
            + list(numpy.linspace(lr / 10, 1e-6, ep - 2 * cyc_ep))
        )
        base_v1_args["global_stats"] = {
            "mean": global_mean,
            "stddev": global_std,
            "use_legacy_version": use_legacy_stats,
        }
        base_v1_args["pretrain_reps"] = pretrain_reps
        base_v1_args["pretrain_opts"]["ignored_keys_for_reduce_dim"] = ["conv_kernel_size"]
        base_v1_args["encoder_args"].dropout = enc_drop
        base_v1_args["encoder_args"].dropout_in = enc_drop
        base_v1_args["encoder_args"].att_dropout = enc_drop
        base_v1_args["decoder_args"].use_zoneout_output = True
        exp_name = f"base_bpe1000_peakLR{lr}_ep{ep}_globalNorm_epochOCLR_pre{pretrain_reps}_fixZoneout_encDrop{enc_drop}_woDepthConvPre"
        return base_v1_args, exp_name

    # # monotonic att weights loss
    # for monotonic_att_loss in ["l1"]:
    #     for scale in [1e-2, 2e-2, 3e-2, 5e-2]:
    #         args, exp_name = get_base_v1_args(8e-4, 50 * 4)
    #         args["decoder_args"].monotonic_att_weights_loss = monotonic_att_loss
    #         args["decoder_args"].monotonic_att_weights_loss_scale = scale
    #         train_job, train_data = run_exp(
    #             exp_name + f"_monotonicAttLoss{scale}_{monotonic_att_loss}",
    #             args,
    #             num_epochs=50 * 4,
    #             epoch_wise_filter=None,
    #             bpe_size=BPE_1K,
    #             partition_epoch=4,
    #             avg_key="dev_score_output/monotonic_att_weights_loss",
    #         )
    #
    #         if scale == 1e-2 and monotonic_att_loss == "l1":
    #             for testset in ["dev"]:
    #                 for beam_size in [4, 8, 12, 24]:
    #                     for thre in [0.0, 0.003]:
    #                         args = copy.deepcopy(args)
    #                         args["decoder_args"].monotonic_att_weights_loss_scale_in_recog = thre
    #                         args["beam_size"] = beam_size
    #                         search_data = get_test_dataset_tuples(BPE_1K)
    #                         run_single_search(
    #                             exp_name
    #                             + f"_monotonicAttLoss{scale}_{monotonic_att_loss}/monotonicLoss/avg/{testset}/thre{thre}_beam{beam_size}",
    #                             train_data,
    #                             search_args=args,
    #                             checkpoint=train_job_avg_ckpt[
    #                                 exp_name + f"_monotonicAttLoss{scale}_{monotonic_att_loss}"
    #                             ],
    #                             feature_extraction_net=log10_net_10ms,
    #                             recog_dataset=search_data[testset][0],
    #                             recog_ref=search_data[testset][1],
    #                             recog_bliss=search_data[testset][2],
    #                         )

    # baseline
    base_v1_args, exp_name = get_base_v1_args(8e-4, 100 * 4, enc_drop=0.15)
    args = copy.deepcopy(base_v1_args)
    args["encoder_args"].num_blocks = 12
    args["encoder_args"].mhsa_weight_dropout = 0.1
    args["encoder_args"].ff_weight_dropout = 0.1
    args["encoder_args"].conv_weight_dropout = 0.1

    args["decoder_args"].embed_dim = 256
    args["decoder_args"].att_dropout = 0.0

    name = exp_name + f"_weightDrop{0.1}_decAttDrop{0.0}_embedDim{256}_numBlocks{12}"
    _, train_data = run_exp(
        name,
        args,
        num_epochs=100 * 4,
        epoch_wise_filter=None,
        bpe_size=BPE_1K,
        partition_epoch=4,
    )
    best_model_name = name

    for ep in [20 * 4]:
        for const_ep in [2 * 4]:
            for lr in [1e-4, 2e-4, 3e-4]:
                for loss_type, lb_scale, ub_scale, ub_limit in [
                    ("l1", 1e-3, 0.0, 20),
                    ("l1", 1e-3, 1e-3, 20),
                    ("l1", 0.8, 0.2, 20),
                    ("l1", 0.8, 0.2, 10),
                    ("l1", 0.8, 0.2, 5),
                    ("l1", 1.0, 1.0, 20),
                    #
                    ("l2", 1.0, 1.0, 20),
                    ("l2", 0.8, 0.2, 20),
                    #
                ]:
                    retrain_args = copy.deepcopy(args)
                    retrain_args["retrain_checkpoint"] = train_job_avg_ckpt[best_model_name]
                    retrain_args["decoder_args"].monotonic_att_weights_loss_opts = {
                        "lb_scale": lb_scale,
                        "ub_scale": ub_scale,
                        "ub_limit": ub_limit,
                        "loss_type": loss_type,
                    }
                    exp_name = best_model_name
                    exp_name += f"_monotonicAttLoss_lb{lb_scale}"
                    if ub_scale:
                        exp_name += f"_ub{ub_scale}-{ub_limit}"
                    exp_name += f"_{loss_type}"

                    # override oclr
                    retrain_args["learning_rates_list"] = [lr] * const_ep + list(
                        numpy.linspace(lr, 1e-6, ep - const_ep)
                    )
                    exp_name += f"_lr{lr}_constEp{const_ep}_retrain{ep}"
                    run_exp(
                        exp_name,
                        retrain_args,
                        num_epochs=ep,
                        epoch_wise_filter=None,
                        bpe_size=BPE_1K,
                        partition_epoch=4,
                    )

                    # *_monotonicAttLoss_lb1.0_ub1.0-20_l1_lr0.0002_constEp8_retrain80
                    # 7.28    6.81  avg
                    if lr == 2e-4 and loss_type == "l1" and lb_scale == 1.0 and ub_scale == 1.0 and ub_limit == 20:
                        for beam_size in [12]:
                            for testset in ["dev"]:
                                for lb_scale in [0.001, 0.005, 0.01, 0.05, 0.1]:
                                    for ub_scale in [0.0]:
                                        search_args = copy.deepcopy(retrain_args)
                                        search_args["beam_size"] = beam_size
                                        search_args["decoder_args"].use_monotonic_att_weights_loss_in_recog = True
                                        search_args["decoder_args"].monotonic_att_weights_loss_opts = {
                                            "lb_scale": lb_scale,
                                            "ub_scale": ub_scale,
                                            "ub_limit": ub_limit,
                                            "loss_type": loss_type,
                                        }
                                        search_data = get_test_dataset_tuples(BPE_1K)
                                        suffix = f"lb{lb_scale}"
                                        if ub_scale:
                                            suffix += f"_ub{ub_scale}-{ub_limit}"
                                        run_single_search(
                                            exp_name + f"/monotonicLoss/avg/{testset}/{suffix}_beam{beam_size}",
                                            train_data,
                                            search_args=search_args,
                                            checkpoint=train_job_avg_ckpt[exp_name],
                                            feature_extraction_net=log10_net_10ms,
                                            recog_dataset=search_data[testset][0],
                                            recog_ref=search_data[testset][1],
                                            recog_bliss=search_data[testset][2],
                                        )

    for ep in [20 * 4]:
        for const_ep in [2 * 4]:
            for lr in [2e-4]:
                for loss_type, lb_scale, ub_scale, ub_limit in [
                    ("l1", 1.0, 1.0, 20),
                    ("l1", 1.0, 1.0, 30),
                    ("l1", 1.0, 1.0, 40),
                    ("exp", 1.0, 1.0, 20),
                    ("log", 1.0, 1.0, 20),
                ]:
                    retrain_args = copy.deepcopy(args)
                    retrain_args["retrain_checkpoint"] = train_job_avg_ckpt[best_model_name]
                    retrain_args["decoder_args"].monotonic_att_weights_loss_opts = {
                        "lb_scale": lb_scale,
                        "ub_scale": ub_scale,
                        "ub_limit": ub_limit,
                        "loss_type": loss_type,
                    }
                    exp_name = best_model_name
                    exp_name += f"_monotonicAttLoss_lb{lb_scale}"
                    if ub_scale:
                        exp_name += f"_ub{ub_scale}-{ub_limit}"
                    exp_name += f"_{loss_type}"

                    # override oclr
                    retrain_args["learning_rates_list"] = [lr] * const_ep + list(
                        numpy.linspace(lr, 1e-6, ep - const_ep)
                    )
                    exp_name += f"_lr{lr}_constEp{const_ep}_retrain{ep}"
                    run_exp(
                        exp_name,
                        retrain_args,
                        num_epochs=ep,
                        epoch_wise_filter=None,
                        bpe_size=BPE_1K,
                        partition_epoch=4,
                    )
