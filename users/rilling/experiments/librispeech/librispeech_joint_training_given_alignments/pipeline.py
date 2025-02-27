from sisyphus import tk
import copy
import os
from i6_core.returnn import ReturnnTrainingJob
from i6_core.returnn.forward import ReturnnForwardJob, ReturnnForwardJobV2
from i6_core.returnn.search import SearchBPEtoWordsJob

from i6_experiments.users.rossenbach.common_setups.returnn.datasets import GenericDataset

from i6_experiments.users.rossenbach.tts.evaluation.nisqa import NISQAMosPredictionJob

from .default_tools import SCTK_BINARY_PATH, NISQA_REPO

def training(config, returnn_exe, returnn_root, prefix, num_epochs=65):
    train_job = ReturnnTrainingJob(
        config,
        log_verbosity=5,
        num_epochs=num_epochs,
        time_rqmt=100,
        mem_rqmt=10,
        cpu_rqmt=4,
        returnn_python_exe=returnn_exe,
        returnn_root=returnn_root,
    )
    train_job.add_alias(prefix + "/training")
    tk.register_output(prefix + "/training.models", train_job.out_model_dir)

    return train_job


def forward(
    checkpoint,
    config,
    returnn_exe,
    returnn_root,
    prefix,
    alias_addition=None,
    target="audio",
    extra_evaluation_epoch=None,
    joint_data=False,
):
    hdf_outputs = [] if target != "audio" else ["/var/tmp/lukas.rilling/out"]
    if target == "audio":
        hdf_outputs = ["/var/tmp/lukas.rilling/out"]
    elif target == "latent_space":
        hdf_outputs = ["samples.hdf", "mean.hdf"]
        # hdf_outputs = ["samples.hdf"]
    else:
        hdf_outputs = []

    last_forward_job = ReturnnForwardJob(
        model_checkpoint=checkpoint,
        returnn_config=config,
        hdf_outputs=hdf_outputs,
        returnn_python_exe=returnn_exe,
        returnn_root=returnn_root,
        mem_rqmt=20,
    )

    # last_forward_job.rqmt["gpu_mem"] = 24

    forward_prefix = prefix + "/forward"

    if target != "audio":
        forward_prefix += f"_{target}"

    if extra_evaluation_epoch is not None:
        forward_prefix += f"_extra_evaluation_{extra_evaluation_epoch}"

    if alias_addition:
        forward_prefix += alias_addition

    forward_suffix = f"/{target}"

    last_forward_job.add_alias(forward_prefix)

    tts_hdf = None

    if target == "audio":
        tts_hdf = last_forward_job.out_hdf_files["/var/tmp/lukas.rilling/out"]
        tk.register_output(forward_prefix + forward_suffix, tts_hdf)
    elif target == "latent_space":
        samples_hdf = last_forward_job.out_hdf_files["samples.hdf"]
        mean_hdf = last_forward_job.out_hdf_files["mean.hdf"]
        tk.register_output(forward_prefix + forward_suffix + "/samples", samples_hdf)
        tk.register_output(forward_prefix + forward_suffix + "/mean", mean_hdf)
    else:
        tts_hdf = last_forward_job.out_hdf_files["output.hdf"]
        tk.register_output(forward_prefix + forward_suffix, tts_hdf)

    return last_forward_job


@tk.block()
def search_single(
    prefix_name,
    returnn_config,
    checkpoint,
    recognition_dataset: GenericDataset,
    recognition_bliss_corpus,
    returnn_exe,
    returnn_root,
    mem_rqmt=8,
):
    """
    Run search for a specific test dataset

    :param str prefix_name:
    :param ReturnnConfig returnn_config:
    :param Checkpoint checkpoint:
    :param returnn_standalone.data.datasets.dataset.GenericDataset recognition_dataset:
    :param Path recognition_reference: Path to a py-dict format reference file
    :param Path returnn_exe:
    :param Path returnn_root:
    """
    returnn_config = copy.deepcopy(returnn_config)
    returnn_config.config["forward"] = recognition_dataset.as_returnn_opts()
    search_job = ReturnnForwardJob(
        model_checkpoint=checkpoint,
        returnn_config=returnn_config,
        log_verbosity=5,
        mem_rqmt=mem_rqmt,
        time_rqmt=4,
        returnn_python_exe=returnn_exe,
        returnn_root=returnn_root,
        hdf_outputs=["search_out.py"],
        device="cpu",
    )
    search_job.add_alias(prefix_name + "/search_job")

    search_words = SearchBPEtoWordsJob(search_job.out_hdf_files["search_out.py"]).out_word_search_results

    from i6_core.returnn.search import SearchWordsToCTMJob
    from i6_core.corpus.convert import CorpusToStmJob
    from i6_core.recognition.scoring import ScliteJob

    search_ctm = SearchWordsToCTMJob(
        recog_words_file=search_words,
        bliss_corpus=recognition_bliss_corpus,
    ).out_ctm_file

    stm_file = CorpusToStmJob(bliss_corpus=recognition_bliss_corpus).out_stm_path

    sclite_job = ScliteJob(ref=stm_file, hyp=search_ctm, sctk_binary_path=SCTK_BINARY_PATH)
    tk.register_output(prefix_name + "/sclite/wer", sclite_job.out_wer)
    tk.register_output(prefix_name + "/sclite/report", sclite_job.out_report_dir)

    return sclite_job.out_wer


@tk.block()
def search(prefix_name, returnn_config, checkpoint, test_dataset_tuples, returnn_exe, returnn_root):
    """

    :param str prefix_name:
    :param ReturnnConfig returnn_config:
    :param Checkpoint checkpoint:
    :param test_dataset_tuples:
    :param returnn_exe:
    :param returnn_root:
    :return:
    """
    # use fixed last checkpoint for now, needs more fine-grained selection / average etc. here
    wers = {}
    for key, (test_dataset, test_dataset_reference) in test_dataset_tuples.items():
        wers[key] = search_single(
            prefix_name + "/%s" % key,
            returnn_config,
            checkpoint,
            test_dataset,
            test_dataset_reference,
            returnn_exe,
            returnn_root,
        )

    from i6_core.report import GenerateReportStringJob

    clean_prefix_name = prefix_name.replace(".", "_")
    format_string_report = ",".join(["{%s_val}" % (clean_prefix_name + key) for key in test_dataset_tuples.keys()])
    format_string = " - ".join(
        ["{%s}: {%s_val}" % (clean_prefix_name + key, clean_prefix_name + key) for key in test_dataset_tuples.keys()]
    )
    values = {}
    values_report = {}
    for key in test_dataset_tuples.keys():
        values[clean_prefix_name + key] = key
        values["%s_val" % (clean_prefix_name + key)] = wers[key]
        values_report["%s_val" % (clean_prefix_name + key)] = wers[key]

    report = GenerateReportStringJob(report_values=values, report_template=format_string, compress=False).out_report
    tk.register_output(os.path.join(prefix_name, "report"), report)
    return format_string_report, values_report


def compute_phoneme_pred_accuracy(
    prefix_name,
    returnn_config,
    checkpoint,
    recognition_datasets,
    returnn_exe,
    returnn_root,
    mem_rqmt=8,
):
    """Replaces the search job for the "encoding_test" experiments, where a simple model is asked
    to predict the phonemes from the latent variables of a glowTTS setup. These experiments output an hdf with
    the total accuracy on each batch and there is no need to perform a search on these.

    :param _type_ prefix_name: _description_
    :param _type_ returnn_config: _description_
    :param _type_ checkpoint: _description_
    :param GenericDataset recognition_dataset: _description_
    :param _type_ recognition_bliss_corpus: _description_
    :param _type_ returnn_exe: _description_
    :param _type_ returnn_root: _description_
    :param int mem_rqmt: _description_, defaults to 8
    """
    jobs = []
    for key, (recognition_dataset, test_dataset_reference) in recognition_datasets.items():

        returnn_config = copy.deepcopy(returnn_config)
        returnn_config.config["forward"] = recognition_dataset.as_returnn_opts()
        search_job = ReturnnForwardJob(
            model_checkpoint=checkpoint,
            returnn_config=returnn_config,
            log_verbosity=5,
            mem_rqmt=mem_rqmt,
            time_rqmt=4,
            returnn_python_exe=returnn_exe,
            returnn_root=returnn_root,
            device="cpu",
        )
        search_job.add_alias(prefix_name + f"/phoneme_pred/{key}")
        tk.register_output(prefix_name + f"/phoneme_pred/{key}", search_job.out_hdf_files["output.hdf"])
        jobs.append(search_job)
    return jobs


def tts_eval(prefix_name, returnn_config, checkpoint, returnn_exe, returnn_root, mem_rqmt=12, vocoder="univnet"):
    """
    Run search for a specific test dataset

    :param prefix_name: prefix folder path for alias and output files
    :param returnn_config: the RETURNN config to be used for forwarding
    :param Checkpoint checkpoint: path to RETURNN PyTorch model checkpoint
    :param returnn_exe: The python executable to run the job with (when using container just "python3")
    :param returnn_root: Path to a checked out RETURNN repository
    :param mem_rqmt: override the default memory requirement
    """
    forward_job = ReturnnForwardJobV2(
        model_checkpoint=checkpoint,
        returnn_config=returnn_config,
        log_verbosity=5,
        mem_rqmt=mem_rqmt,
        time_rqmt=2,
        device="cpu",
        cpu_rqmt=4,
        returnn_python_exe=returnn_exe,
        returnn_root=returnn_root,
        output_files=["audio_files", "out_corpus.xml.gz"],
    )
    forward_job.add_alias(prefix_name + f"/tts_eval_{vocoder}/forward")
    evaluate_nisqa(prefix_name, forward_job.out_files["out_corpus.xml.gz"], vocoder=vocoder)
    return forward_job


def evaluate_nisqa(prefix_name: str, bliss_corpus: tk.Path, vocoder: str = "univnet"):
    predict_mos_job = NISQAMosPredictionJob(bliss_corpus, nisqa_repo=NISQA_REPO)
    predict_mos_job.add_alias(prefix_name + f"/tts_eval_{vocoder}/nisqa_mos")
    tk.register_output(
        os.path.join(prefix_name, f"tts_eval_{vocoder}/nisqa_mos/average"), predict_mos_job.out_mos_average
    )
    tk.register_output(os.path.join(prefix_name, f"tts_eval_{vocoder}/nisqa_mos/min"), predict_mos_job.out_mos_min)
    tk.register_output(os.path.join(prefix_name, f"tts_eval_{vocoder}/nisqa_mos/max"), predict_mos_job.out_mos_max)
    tk.register_output(
        os.path.join(prefix_name, f"tts_eval_{vocoder}/nisqa_mos/std_dev"), predict_mos_job.out_mos_std_dev
    )
