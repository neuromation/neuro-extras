import logging
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Optional, Sequence

import click
import neuro_sdk as neuro_api
from neuro_cli.asyncio_utils import run as run_async
from neuro_cli.const import EX_OK, EX_PLATFORMERROR
from neuro_sdk.parsing_utils import _as_repo_str
from neuro_sdk.url_utils import uri_from_cli

from .cli import main
from .common import _attach_job_stdout
from .image_builder import ImageBuilder
from .utils import get_neuro_client


logger = logging.getLogger(__name__)


@main.group()
def image() -> None:
    """
    Job container image operations.
    """
    pass


@image.command("transfer")
@click.argument("source")
@click.argument("destination")
@click.option(
    "-F",
    "--force-overwrite",
    default=False,
    is_flag=True,
    help="Transfer even if the destination image already exists.",
)
def image_transfer(source: str, destination: str, force_overwrite: bool) -> None:
    """
    Copy images between clusters.
    """
    exit_code = run_async(_image_transfer(source, destination, force_overwrite))
    sys.exit(exit_code)


@image.command(
    "build", help="Build Job container image remotely on cluster using Kaniko."
)
@click.option("-f", "--file", default="Dockerfile")
@click.option("--build-arg", multiple=True)
@click.option(
    "-v",
    "--volume",
    metavar="MOUNT",
    multiple=True,
    help=(
        "Mounts directory from vault into container. "
        "Use multiple options to mount more than one volume. "
        "Use --volume=ALL to mount all accessible storage directories."
    ),
)
@click.option(
    "-e",
    "--env",
    metavar="VAR=VAL",
    multiple=True,
    help=(
        "Set environment variable in container "
        "Use multiple options to define more than one variable"
    ),
)
@click.option(
    "-s",
    "--preset",
    metavar="PRESET",
    help=(
        "Predefined resource configuration (to see available values, "
        "run `neuro config show`)"
    ),
)
@click.option(
    "-F",
    "--force-overwrite",
    default=False,
    is_flag=True,
    help="Build even if the destination image already exists.",
)
@click.option(
    "--cache/--no-cache",
    default=True,
    show_default=True,
    help="Use kaniko cache while building image",
)
@click.argument("path")
@click.argument("image_uri")
@click.option("--verbose", type=bool, default=False)
def image_build(
    file: str,
    build_arg: Sequence[str],
    path: str,
    image_uri: str,
    volume: Sequence[str],
    env: Sequence[str],
    preset: str,
    force_overwrite: bool,
    cache: bool,
    verbose: bool,
) -> None:
    try:
        sys.exit(
            run_async(
                _build_image(
                    dockerfile_path=file,
                    context=path,
                    image_uri=image_uri,
                    use_cache=cache,
                    build_args=build_arg,
                    volume=volume,
                    env=env,
                    preset=preset,
                    force_overwrite=force_overwrite,
                    verbose=verbose,
                )
            )
        )
    except (ValueError, click.ClickException) as e:
        logger.error(f"Failed to build image: {e}")
        sys.exit(EX_PLATFORMERROR)


async def _parse_neuro_image(image: str) -> neuro_api.RemoteImage:
    async with get_neuro_client() as client:
        return client.parse.remote_image(image)


def _get_cluster_from_uri(image_uri: str, *, scheme: str) -> Optional[str]:
    uri = uri_from_cli(image_uri, "", "", allowed_schemes=[scheme])
    return uri.host


async def _image_transfer(src_uri: str, dst_uri: str, force_overwrite: bool) -> int:
    src_cluster: Optional[str] = _get_cluster_from_uri(src_uri, scheme="image")
    dst_cluster: Optional[str] = _get_cluster_from_uri(dst_uri, scheme="image")
    if not dst_cluster:
        raise ValueError(f"Invalid destination image {dst_uri}: missing cluster name")

    with tempfile.TemporaryDirectory() as tmpdir:
        async with get_neuro_client(cluster=src_cluster) as src_client:
            src_image = src_client.parse.remote_image(image=src_uri)
            src_client_config = src_client.config

        dockerfile = Path(f"{tmpdir}/Dockerfile")
        dockerfile.write_text(
            textwrap.dedent(
                f"""\
                FROM {_as_repo_str(src_image)}
                LABEL neu.ro/source-image-uri={src_uri}
                """
            )
        )
        return await _build_image(
            dockerfile_path=dockerfile.name,
            context=tmpdir,
            image_uri=dst_uri,
            use_cache=True,
            build_args=[],
            volume=[],
            env=[],
            force_overwrite=force_overwrite,
            other_client_configs=[src_client_config],
        )


async def _build_image(
    dockerfile_path: str,
    context: str,
    image_uri: str,
    use_cache: bool,
    build_args: Sequence[str],
    volume: Sequence[str],
    env: Sequence[str],
    force_overwrite: bool,
    preset: Optional[str] = None,
    other_client_configs: Sequence[neuro_api.Config] = (),
    verbose: bool = False,
) -> int:
    cluster = _get_cluster_from_uri(image_uri, scheme="image")
    async with get_neuro_client(cluster=cluster) as client:
        if not preset:
            preset = next(iter(client.config.presets.keys()))
        job_preset = client.config.presets[preset]
        context_uri = uri_from_cli(
            context,
            client.username,
            client.cluster_name,
            allowed_schemes=("file", "storage"),
        )
        target_image = await _parse_neuro_image(image_uri)
        try:
            existing_images = await client.images.tags(
                neuro_api.RemoteImage(
                    name=target_image.name,
                    owner=target_image.owner,
                    cluster_name=target_image.cluster_name,
                    registry=target_image.registry,
                    tag=None,
                )
            )
        except neuro_api.errors.ResourceNotFound:
            # target_image does not exists on platform registry, skip else block
            pass
        else:
            if target_image in existing_images and force_overwrite:
                logger.warning(
                    f"Target image '{target_image}' exists and will be overwritten."
                )
            elif target_image in existing_images and not force_overwrite:
                raise click.ClickException(
                    f"Target image '{target_image}' exists. "
                    f"Use -F/--force-overwrite flag to enforce overwriting."
                )

        builder = ImageBuilder(
            client, other_clients_configs=other_client_configs, verbose=verbose
        )
        job = await builder.launch(
            dockerfile_path=dockerfile_path,
            context_uri=context_uri,
            image_uri_str=image_uri,
            use_cache=use_cache,
            build_args=build_args,
            volume=volume,
            env=env,
            job_preset=job_preset,
        )
        exit_code = await _attach_job_stdout(job, client, name="builder")
        if exit_code == EX_OK:
            logger.info(f"Successfully built {image_uri}")
            return EX_OK
        else:
            raise click.ClickException(f"Failed to build image: {exit_code}")
