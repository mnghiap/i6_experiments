import copy
from typing import Any, Dict

from i6_core.returnn.config import ReturnnConfig, CodeWrapper

from i6_experiments.common.setups.returnn_pytorch.serialization import (
    Collection as TorchCollection,
)
from i6_experiments.common.setups.serialization import Import
from ..data import TrainingDatasets
from ..flashlight_phon_ctc.serializer import get_pytorch_serializer_v3, PACKAGE


def get_training_config(
    training_datasets: TrainingDatasets,
    network_module: str,
    net_args: Dict[str, Any],
    config: Dict[str, Any],
    debug: bool = False,
    use_custom_engine=False,
    use_speed_perturbation=False,
):
    """
    Returns the RETURNN config serialized by :class:`ReturnnCommonSerializer` in returnn_common for the ctc_aligner
    :param returnn_common_root: returnn_common version to be used, usually output of CloneGitRepositoryJob
    :param training_datasets: datasets for training
    :param kwargs: arguments to be passed to the network construction
    :return: RETURNN training config
    """

    # changing these does not change the hash
    post_config = {
        "cleanup_old_models": True,
        "stop_on_nonfinite_train_score": True,  # this might break now with True
        "num_workers_per_gpu": 2,
    }

    base_config = {
        "max_seqs": 60,
        #############
        "train": copy.deepcopy(training_datasets.train.as_returnn_opts()),
        "dev": training_datasets.cv.as_returnn_opts(),
        "eval_datasets": {"devtrain": training_datasets.devtrain.as_returnn_opts()},
    }
    config = {**base_config, **copy.deepcopy(config)}
    post_config["backend"] = "torch"

    serializer = get_pytorch_serializer_v3(
        network_module=network_module, net_args=net_args, debug=debug, use_custom_engine=use_custom_engine
    )
    python_prolog = None
    if use_speed_perturbation:
        prolog_serializer = TorchCollection(
            serializer_objects=[
                Import(
                    code_object_path=PACKAGE + ".dataset_code.speed_perturbation.legacy_speed_perturbation",
                    unhashed_package_root=PACKAGE,
                )
            ]
        )
        python_prolog = [prolog_serializer]
        config["train"]["datasets"]["zip_dataset"]["audio"]["pre_process"] = CodeWrapper("legacy_speed_perturbation")

    returnn_config = ReturnnConfig(
        config=config, post_config=post_config, python_prolog=python_prolog, python_epilog=[serializer]
    )
    return returnn_config


def get_prior_config(
    training_datasets: TrainingDatasets,
    network_module: str,
    net_args: Dict[str, Any],
    config: Dict[str, Any],
    debug: bool = False,
    use_custom_engine=False,
    **kwargs,
):
    """
    Returns the RETURNN config serialized by :class:`ReturnnCommonSerializer` in returnn_common for the ctc_aligner
    :param returnn_common_root: returnn_common version to be used, usually output of CloneGitRepositoryJob
    :param training_datasets: datasets for training
    :param kwargs: arguments to be passed to the network construction
    :return: RETURNN training config
    """

    # changing these does not change the hash
    post_config = {}

    base_config = {
        #############
        "batch_size": 50000 * 160,
        "max_seqs": 60,
        #############
        "forward": training_datasets.prior.as_returnn_opts(),
    }
    config = {**base_config, **copy.deepcopy(config)}
    post_config["backend"] = "torch"

    serializer = get_pytorch_serializer_v3(
        network_module=network_module,
        net_args=net_args,
        debug=debug,
        use_custom_engine=use_custom_engine,
        prior=True,
    )
    returnn_config = ReturnnConfig(config=config, post_config=post_config, python_epilog=[serializer])
    return returnn_config


def get_search_config(
    network_module: str,
    net_args: Dict[str, Any],
    decoder: [str],
    decoder_args: Dict[str, Any],
    config: Dict[str, Any],
    debug: bool = False,
    use_custom_engine=False,
    **kwargs,
):
    """
    Returns the RETURNN config serialized by :class:`ReturnnCommonSerializer` in returnn_common for the ctc_aligner
    :param returnn_common_root: returnn_common version to be used, usually output of CloneGitRepositoryJob
    :param training_datasets: datasets for training
    :param kwargs: arguments to be passed to the network construction
    :return: RETURNN training config
    """

    # changing these does not change the hash
    post_config = {}

    base_config = {
        #############
        "batch_size": 24000 * 160,
        "max_seqs": 60,
        #############
        # dataset is added later in the pipeline during search_single
    }
    config = {**base_config, **copy.deepcopy(config)}
    post_config["backend"] = "torch"

    serializer = get_pytorch_serializer_v3(
        network_module=network_module,
        net_args=net_args,
        debug=debug,
        use_custom_engine=use_custom_engine,
        decoder=decoder,
        decoder_args=decoder_args,
    )
    returnn_config = ReturnnConfig(config=config, post_config=post_config, python_epilog=[serializer])
    return returnn_config
