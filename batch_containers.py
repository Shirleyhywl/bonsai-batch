#! /usr/bin/env python
"""Run and scale simulation experiments on Azure Batch."""

# TODO: 1. Add monitoring tasks
# TODO: 2. Use [batch-insights](https://github.com/Azure/batch-insights) for monitoring

import configparser
import datetime
import os
import pathlib
import sys
import time
from typing import List
from distutils.util import strtobool

import azure.batch._batch_service_client as batch
import azure.batch.batch_auth as batch_auth
import azure.batch.models as batchmodels
import blobxfer.api
import fire
from azure.common.credentials import ServicePrincipalCredentials
from dotenv import load_dotenv, set_key
from batch_creation import user_config, windows_config

import xfer_utils

import logging
import logging.handlers

if not pathlib.Path("logs").exists():
    pathlib.Path("logs").mkdir(exist_ok=True, parents=True)

logging.getLogger().setLevel(logging.NOTSET)

# Add stdout handler, with level INFO
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.INFO)
formater = logging.Formatter("%(name)-13s: %(levelname)-8s %(message)s")
console.setFormatter(formater)
logging.getLogger().addHandler(console)

# Add file rotating handler, with level DEBUG
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger(__name__)


class AzureBatchContainers(object):
    def __init__(
        self,
        config_file: str = user_config,
        service_principal: bool = False,
        workspace: str = None,
        access_key: str = None,
    ):
        """Sim-Scaling with Azure Batch Containers and Azure Container Registry.

        Parameters
        ----------
        config_file : str, optional
            Location of your configuration settings. This should include your Batch account settings, Container Registry, and Bonsai credentials. (the default is "config.ini", which is relative to your current path.)

        """

        # parse config from config_file
        # can overwrite config values by accessing self.config
        self.config_file = config_file
        self.config = configparser.ConfigParser()
        if not pathlib.Path(self.config_file).exists():
            raise ValueError("No config file found at {0}".format(self.config_file))
        else:
            logger.debug("Using config from {}".format(self.config_file))
            self.config.read(self.config_file)
            self.get_container_registry()
            self.get_image_ref()

            if service_principal:
                logger.info("Authenticating with service principal...")
                tenant_id = self.config["SERVICE"]["TENANT_ID"]
                client_id = self.config["SERVICE"]["CLIENT_ID"]
                secret = self.config["SERVICE"]["SECRET"]
            else:
                tenant_id = None
                client_id = None
                secret = None

            self.authenticate_batch(
                service_principal=service_principal,
                tenant_id=tenant_id,
                client_id=client_id,
                secret=secret,
            )

            if not all([workspace, access_key]):
                workspace, access_key = load_bonsai_env(".env")
            self.workspace = workspace
            self.access_key = access_key
            # pool needs to be created before fileshare can be activated
            self.use_fileshare = False

    def get_container_registry(self):
        """Creates an attribute called registry which attaches to your ACR account provided in config.

        Returns
        -------
        azure.batch.models.ContainerRegistry
            Saves to attribute self.registry
        """

        self.image_name = "/".join(
            [
                self.config["ACR"]["SERVER"].strip("'"),
                self.config["ACR"]["IMAGE_NAME"].strip("'"),
            ]
        )
        self.image_version = self.config["ACR"]["IMAGE_VERSION"].strip("'")

        self.registry = batch.models.ContainerRegistry(
            registry_server=self.config["ACR"]["SERVER"].strip("'"),
            user_name=self.config["ACR"]["USERNAME"].strip("'"),
            password=self.config["ACR"]["PASSWORD"].strip("'"),
        )

        return self.registry

    def get_image_ref(self):
        """Get reference image for the Batch pool. All parameters are pooled from config['POOL'], which should include keys for PUBLISHER, OFFER, SKU and VERSION. Image reference is stored in self.image_ref_to_use

        Returns
        -------
        ImageReference
        """

        # [See list of VM images](https://docs.microsoft.com/en-us/azure/batch/batch-linux-nodes#list-of-virtual-machine-images)
        self.image_ref_to_use = batch.models.ImageReference(
            publisher=self.config["POOL"]["PUBLISHER"].strip("'"),
            offer=self.config["POOL"]["OFFER"].strip("'"),
            sku=self.config["POOL"]["SKU"].strip("'"),
            version=self.config["POOL"]["VERSION"].strip("'"),
        )

        return self.image_ref_to_use

    def authenticate_batch(
        self,
        service_principal: bool = False,
        tenant_id: str = None,
        client_id: str = None,
        secret: str = None,
    ):
        """Authenticate to Batch service using credential provided in config['BATCH'], and saves batch client to self.batch_client.

        Returns
        -------
        azure.batch.BatchServiceClient
            Authenticated Azure Batch Service client.
        """

        batch_account_name = (self.config["BATCH"]["ACCOUNT_NAME"].strip("'"),)
        location = self.config["GROUP"]["LOCATION"].strip("'")

        if service_principal:
            RESOURCE = "https://batch.core.windows.net/"
            BATCH_ACCOUNT_URL = "https://{0}.{1}.batch.azure.com".format(
                batch_account_name, location
            )

            credentials = ServicePrincipalCredentials(
                client_id=client_id, secret=secret, tenant=tenant_id, resource=RESOURCE
            )

        else:
            credentials = batch_auth.SharedKeyCredentials(
                self.config["BATCH"]["ACCOUNT_NAME"].strip("'"),
                self.config["BATCH"]["ACCOUNT_KEY"].strip("'"),
            )

        self.batch_client = batch.BatchServiceClient(
            credentials, self.config["BATCH"]["ACCOUNT_URL"].strip("'")
        )

        return self.batch_client

    def create_pool(self, skip_if_exists=True, use_fileshare: bool = True):
        """Create an Azure Batch Pool. All necessary parameters should be listed in config['POOL'], and saves pool to self.pool_id.

        Parameters
        ----------
        skip_if_exists : bool, optional
            Skip creation of pool if it already exists (the default is True, which means pool will be re-used)

        """
        pool_id = self.config["POOL"]["POOL_ID"].strip("'")
        pool_vm_size = self.config["POOL"]["VM_SIZE"].strip("'")
        num_tasks_per_node = int(self.config["POOL"]["TASKS_PER_NODE"])
        pool_low_priority_node_count = int(self.config["POOL"]["LOW_PRI_NODES"])
        pool_dedicated_node_count = int(self.config["POOL"]["DEDICATED_NODES"])
        node_agent_sku = self.config["POOL"]["AGENT_SKU"].strip("'")
        self.use_fileshare = use_fileshare

        container_conf = batch.models.ContainerConfiguration(
            container_image_names=[self.image_name + ":" + self.image_version],
            container_registries=[self.registry],
        )

        if self.config["ACR"]["PLATFORM"] == "windows":
            self.mount_path = "S"
        else:
            self.mount_path = "azfileshare"

        if use_fileshare:
            fileshare_mount = batchmodels.MountConfiguration(
                azure_file_share_configuration=batchmodels.AzureFileShareConfiguration(
                    account_name=self.config["STORAGE"]["ACCOUNT_NAME"],
                    azure_file_url=self.config["STORAGE"]["URL"],
                    account_key=self.config["STORAGE"]["ACCOUNT_KEY"],
                    relative_mount_path=self.mount_path,
                    mount_options="/persistent:Yes",
                )
            )
            logger.info(f"Using fileshare mount {fileshare_mount}")
            fileshare_mount = [fileshare_mount]
        else:
            fileshare_mount = None

        self.new_pool = batch.models.PoolAddParameter(
            id=pool_id,
            virtual_machine_configuration=batch.models.VirtualMachineConfiguration(
                image_reference=self.image_ref_to_use,
                container_configuration=container_conf,
                node_agent_sku_id=node_agent_sku,
            ),
            vm_size=pool_vm_size,
            max_tasks_per_node=num_tasks_per_node,
            target_dedicated_nodes=pool_dedicated_node_count,
            target_low_priority_nodes=pool_low_priority_node_count,
            mount_configuration=fileshare_mount,
        )

        if not skip_if_exists or not self.batch_client.pool.exists(pool_id):
            logger.warning("Creating new pool named {}".format(pool_id))
            self.batch_client.pool.add(self.new_pool)
        else:
            logger.warning("Pool exists, re-using {}".format(pool_id))

        # update pool id for jobs
        self.pool_id = pool_id

    def delete_pool(self, pool_name):

        logger.info("Deleting pool: {0}".format(pool_name))
        self.batch_client.pool.delete(pool_name)

    def add_job(self, job_name: str = None):
        """Add a job to Azure Batch Pool in self.pool_id. Job is specified using config['POOL'] parameters. Job ID is retained to self.job_id attribute."""

        if job_name:
            self.job_id = job_name
        else:
            self.job_id = (
                "Job-"
                + self.config["POOL"]["JOB_NAME"].strip("'")
                + "-"
                + "{:%Y-%m-%d-%H-%M-%S}".format(datetime.datetime.now())
            )
        job = batch.models.JobAddParameter(
            id=self.job_id, pool_info=batch.models.PoolInformation(pool_id=self.pool_id)
        )

        logger.info("Adding job {0} to pool {1}".format(self.job_id, self.pool_id))
        self.batch_client.job.add(job)

    def delete_job(self, job_name: str = None):
        """Deletes a job that already exists in an Azure Batch Pool in self.pool_id. Job is specified using config['POOL'] parameters."""
        self.batch_client.job.delete(job_name)

    def delete_all_tasks(self):
        """Deletes all tasks in given pool"""

        jobs = [l for l in self.batch_client.job.list()]

        def try_delete(jid):
            try:
                self.batch_client.job.delete(job_id=jid)
            except Exception as e:
                print("already gone")

        jid_list = [job.as_dict()["id"] for job in jobs]
        return [try_delete(j_id) for j_id in jid_list]

    def delete_pool(self):

        pool_name = self.config["POOL"]["POOL_ID"]
        logger.info("Deleting pool: {0}".format(pool_name))
        self.batch_client.pool.delete(pool_name)

    def list_pools(self):

        return [i.id for i in self.batch_client.pool.list()]

    def list_tasks(self, job_id):

        self.tasks = batch.task.list(job_id)

    def copy_logfiles(self, file_path: str, encoding):

        self.tasks = batch.task.list(self.job_id)
        for task in self.tasks:

            node_id = batch.task.get(self.job_id, task.id).node_info.node_id
            logger.info("Task: {}".format(task.id))
            logger.info("Node: {}".format(node_id))

            stream = batch.file.get_from_task(self.job_id, task.id, file_path)

            file_text = _read_stream_as_string(stream, encoding)
            logger.info("Standard output:")
            logger.info(file_text)

    def add_task(self, task_command: str, task_name: str, start_dir: str = None):
        """Add tasks to Azure Batch Job.

        Parameters
        ----------
        task_command : str
            Task to run on job. This can be any task to run on the current job_id.
        task_name : str
            Name of task.

        """
        user = batchmodels.UserIdentity(
            auto_user=batchmodels.AutoUserSpecification(
                elevation_level=batchmodels.ElevationLevel.admin,
                scope=batchmodels.AutoUserScope.task,
            )
        )
        if not start_dir:
            start_dir = "src"
        if self.config["POOL"]["PUBLISHER"] == "MicrosoftWindowsServer":
            extra_opts = f"-w C:\\{start_dir}\\"
        else:
            extra_opts = f"--workdir /{start_dir}/"

        if self.use_fileshare:
            if self.config["POOL"]["PUBLISHER"] == "MicrosoftWindowsServer":
                mount = f"S:\\:C:\\{start_dir}\\logs"
            else:
                mount = f"/azfileshare/:/{start_dir}/logs"
            extra_opts += f" --volume {mount}"

        self.task_id = task_name
        logger.debug(
            "Submitting task {0} to pool {1} with command {2}".format(
                task_name, self.pool_id, task_command
            )
        )
        logger.debug(f"Extra configuration operations: {extra_opts}")
        task_container_settings = batch.models.TaskContainerSettings(
            image_name=self.image_name + ":" + self.image_version,
            container_run_options=extra_opts,
        )
        task = batch.models.TaskAddParameter(
            id=self.task_id,
            command_line=task_command,
            container_settings=task_container_settings,
            environment_settings=[
                batchmodels.EnvironmentSetting(
                    name="SIM_WORKSPACE", value=self.workspace
                ),
                batchmodels.EnvironmentSetting(
                    name="SIM_ACCESS_KEY", value=self.access_key
                ),
            ],
            user_identity=user,
        )

        self.batch_client.task.add(self.job_id, task)

    def wait_for_tasks_to_complete(self, timeout):

        timeout_expiration = datetime.datetime.now() + timeout

        logger.info(
            "Monitoring all tasks for 'Completed' state, timeout in {}...".format(
                timeout
            ),
            end="",
        )

        while datetime.datetime.now() < timeout_expiration:
            print(".", end="")
            sys.stdout.flush()
            tasks = self.batch_client.task.list(self.job_id)

            incomplete_tasks = [
                task for task in tasks if task.state != batchmodels.TaskState.completed
            ]
            if not incomplete_tasks:
                print()
                return True
            else:
                time.sleep(1)

        print()
        raise RuntimeError(
            "ERROR: Tasks did not reach 'Completed' state within "
            "timeout period of " + str(timeout)
        )

    def batch_main(
        self,
        command: str = None,
        brain_name: str = None,
        wait_for_tasks: bool = False,
        log_iterations: bool = False,
        workdir: str = None,
    ):
        """Hub to run Bonsai scale-sim job. This adds hub.py tasks to run on the current job_id. The command pulls config['POOL']['PYTHON_EXEC'] and config.['BONSAI'] parameters."""

        self.create_pool(use_fileshare=log_iterations)
        self.add_job()

        if not brain_name:
            brain_name = self.config["BONSAI"]["BRAIN_NAME"].strip("'")

        logger.info(
            "Using batch account {0} to run job {1} with {2} tasks".format(
                self.config["BATCH"]["ACCOUNT_NAME"],
                self.config["POOL"]["JOB_NAME"],
                self.config["POOL"]["NUM_TASKS"],
            )
        )

        for i in range(int(self.config["POOL"]["NUM_TASKS"])):
            logger.debug(
                "Staggering {}s between task".format(
                    int(self.config["POOL"]["TIME_DELAY_BETWEEN_SIMS"])
                )
            )

            if not command:
                command = "python main.py"
            self.add_task(
                task_command=command,
                task_name="job_number{0}_{1}".format(
                    i, self.config["POOL"]["JOB_NAME"].strip("'")
                ),
                start_dir=workdir,
            )

        # Pause execution until tasks reach Completed state.
        if wait_for_tasks:
            self.wait_for_tasks_to_complete(datetime.timedelta(hours=2))
            logger.info(
                "Success! All tasks reached the 'Completed' state within the specified timeout period."
            )
        else:
            logger.info(
                "Submitted all tasks, use self.list_tasks to view currently running tasks."
            )


def _read_stream_as_string(stream, encoding):
    """Read stream as string
    :param stream: input stream generator
    :param str encoding: The encoding of the file. The default is utf-8.
    :return: The file content.
    :rtype: str
    """
    output = io.BytesIO()
    try:
        for data in stream:
            output.write(data)
        if encoding is None:
            encoding = "utf-8"
        return output.getvalue().decode(encoding)
    finally:
        output.close()
    raise RuntimeError("could not write data to stream or decode bytes")


def load_bonsai_env(env_file: str = ".env"):

    env_file_exists = os.path.exists(env_file)
    if not env_file_exists:
        open(".env", "a").close()
        workspace = input("Please enter your workspace id: ")
        set_key(".env", "SIM_WORKSPACE", workspace)
        access_key = input("Please enter your access key: ")
        set_key(".env", "SIM_ACCESS_KEY", access_key)
    else:
        load_dotenv(verbose=True)
        workspace = os.getenv("SIM_WORKSPACE")
        access_key = os.getenv("SIM_ACCESS_KEY")

    return workspace, access_key


def run_tasks(
    task_to_run: str = None,
    workspace: str = None,
    access_key: str = None,
    num_tasks: str = None,
    low_pri_nodes: int = 9,
    dedicated_nodes: int = 1,
    pool_name: str = None,
    job_name: str = None,
    use_service_principal: bool = False,
    vm_sku: str = None,
    config_file: str = user_config,
    log_iterations: bool = False,
    workdir: str = None,
    image_name: str = None,
    image_version: str = None,
):
    """Run simulators in Azure Batch.

    Parameters
    ----------
    num_tasks : str, mandatory
        Number of simulators to run as separate tasks
    brain_name: str, mandatory
        Name of the brain to train
    low_pri_nodes : int, optional
        Number of low priority to create in pool, by default 9
    dedicated_nodes : int, optional
        Number of dedicated to create in pool, by default 1
    pool_name : str, optional
        Name of the pool to create for simulation scaling, by default None
    job_name : str, optional
        Job name for simulation scaling job, by default None
    config_file : str, optional
        Location of configuration file containing ACR and Batch parameters, by default user_config
    """

    if not os.path.exists(config_file):
        raise ValueError(f"No configuration file found at {config_file}")

    config = configparser.ConfigParser()
    config.read(config_file)
    platform = config["ACR"]["PLATFORM"]

    if not task_to_run:
        task_to_run = input(
            "Please enter task to run from container (e.g., python __main__.py): "
        )
    if not num_tasks:
        num_tasks = input("Number of simulators to run as tasks on Batch: ")
    total_nodes = low_pri_nodes + dedicated_nodes
    tasks_per_node = max(int(int(num_tasks) / total_nodes), 1)

    config["POOL"]["NUM_TASKS"] = str(num_tasks)
    config["POOL"]["TASKS_PER_NODE"] = str(tasks_per_node)
    config["POOL"]["LOW_PRI_NODES"] = str(low_pri_nodes)
    config["POOL"]["DEDICATED_NODES"] = str(dedicated_nodes)

    if not vm_sku:
        vm_sku = input(
            "What VM Name / SKU do you want to use? (if you don't know say None): "
        )
        if vm_sku.lower() == "none" or vm_sku.lower() == "":
            if tasks_per_node <= 8:
                vm_sku = "Standard_E2s_v3"
            elif tasks_per_node <= 16:
                vm_sku = "Standard_E8s_v3"
            elif tasks_per_node <= 32:
                vm_sku = "Standard_E16s_v3"
            elif tasks_per_node <= 75:
                vm_sku = "Standard_E32s_v3"
            elif tasks_per_node > 75:
                vm_sku = "Standard_E64s_v3"
                logger.info(
                    "Running {0} tasks per node, please check if VM Size is compatible".format(
                        tasks_per_node
                    )
                )
    config["POOL"]["VM_SIZE"] = vm_sku

    if not image_name:
        image_name = config["ACR"]["IMAGE_NAME"]
    if not image_version:
        image_version = config["ACR"]["IMAGE_VERSION"]

    if not pool_name:
        config["POOL"]["POOL_ID"] = image_name + "pool" + str(total_nodes)
    else:
        config["POOL"]["POOL_ID"] = pool_name
    if not job_name:
        config["POOL"]["JOB_NAME"] = image_name + "job" + str(num_tasks)
    else:
        config["POOL"]["JOB_NAME"] = job_name

    if platform.lower() == "windows":
        win_config = configparser.ConfigParser()
        win_config.read(windows_config)

        config["POOL"]["PUBLISHER"] = win_config["POOL"]["PUBLISHER"]
        config["POOL"]["OFFER"] = win_config["POOL"]["OFFER"]
        config["POOL"]["SKU"] = win_config["POOL"]["SKU"]
        config["POOL"]["VERSION"] = win_config["POOL"]["VERSION"]
        config["POOL"]["AGENT_SKU"] = win_config["POOL"]["AGENT_SKU"]
        config["POOL"]["PYTHON_EXEC"] = "python"

    config["ACR"]["IMAGE_NAME"] = image_name
    config["ACR"]["IMAGE_VERSION"] = image_version

    with open(config_file, "w") as conf_file:
        config.write(conf_file)

    batch_run = AzureBatchContainers(
        config_file=config_file,
        service_principal=use_service_principal,
        workspace=workspace,
        access_key=access_key,
    )
    if image_name:
        batch_run.config["ACR"]["IMAGE_NAME"] = image_name
    if image_version:
        batch_run.config["ACR"]["IMAGE_VERSION"] = image_version

    if type(log_iterations) == str:
        log_iterations = bool(strtobool(log_iterations))

    batch_run.batch_main(
        command=task_to_run, log_iterations=log_iterations, workdir=workdir,
    )


def stop_job(config_file: str = user_config):

    batch_run = AzureBatchContainers(config_file=config_file)
    batch_run.delete_job()


def kill_pool(config_file: str = user_config):

    batch_run = AzureBatchContainers(config_file=config_file)
    batch_run.delete_pool()


def upload_files(directory: str, config_file: str = user_config):
    """Upload files into attached batch storage account.

    Parameters
    ----------
    directory : str
        directory of files to upload to storage container
    config_file : str, optional
        config file containing storage keys, by default 'newconf.ini'
    """

    context = xfer_utils.create_context(config_file=config_file, local_path=directory)

    xfer_utils.start_uploader(context, directory)


def list_pool_nodes(config_file: str = user_config):

    batch_pool = AzureBatchContainers(config_file=config_file)
    pool_id = batch_pool.config["POOL"]["POOL_ID"].strip("'")

    pc = batch_pool.batch_client.account.list_pool_node_counts(
        account_list_pool_node_counts_options=batchmodels.AccountListPoolNodeCountsOptions(
            filter="poolId eq '{}'".format(pool_id)
        )
    )
    try:
        nodes = list(pc)[0]
        return nodes.as_dict()
    except IndexError:
        raise RuntimeError("pool {} does not exist".format(pool_id))


if __name__ == "__main__":

    fire.Fire()
    # nodes = list_pool_nodes(pool_name="PowerMount999")
    # run_tasks(image_name="winhouse")
    # batch_run = AzureBatchContainers(config_file=user_config)
    # batch_run.delete_all_tasks(pool_id="dev2")
    # batch_run.batch_main()

    # next_task = 'python -c "import os; print(os.listdir()); print(os.getcwd())"'
    # batch_run.add_job('dircheck2')
    # batch_run.add_task(next_task, task_name='dir_check2')
    # another_task = r"""python3 -c 'import os; os.chdir("/bonsai"); print(os.listdir()); print(os.getcwd())'"""
    # batch_run.add_task(another_task, task_name='dir_change_again')

    # run_tasks()
