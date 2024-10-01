"""Module for loading the lydata datasets."""

import fnmatch
import logging
import os
import warnings
from collections.abc import Generator, Iterable
from datetime import datetime
from io import TextIOWrapper
from pathlib import Path

import mistletoe
import numpy as np  # noqa: F401
import pandas as pd
from github import Auth, Github
from mistletoe.block_token import Heading
from mistletoe.markdown_renderer import MarkdownRenderer
from mistletoe.token import Token
from pydantic import BaseModel, Field, constr

from lydata import _repo

logger = logging.getLogger(__name__)

low_min1_str = constr(to_lower=True, min_length=1)


class SkipDiskError(Exception):
    """Raised when the user wants to skip loading from disk."""


class SkipGithubError(Exception):
    """Raised when the user wants to skip loading from GitHub."""


class LyDatasetConfig(BaseModel):
    """Specification of a dataset."""

    year: int = Field(
        gt=0,
        le=datetime.now().year,
        description="Release year of dataset.",
    )
    institution: low_min1_str = Field(
        description="Institution's short code. E.g., University Hospital Zurich: `usz`."
    )
    subsite: low_min1_str = Field(description="Subsite(s) this dataset covers.")
    repo: low_min1_str = Field(default=_repo, description="GitHub `repository/owner`.")
    ref: low_min1_str = Field(
        default="main",
        description="Branch/tag/commit of the repo.",
    )

    @property
    def name(self) -> str:
        """Get the name of the dataset.

        >>> conf = LyDatasetConfig(year=2023, institution="clb", subsite="multisite")
        >>> conf.name
        '2023-clb-multisite'
        """
        return f"{self.year}-{self.institution}-{self.subsite}"

    @property
    def path(self) -> Path:
        """Get the path to the dataset.

        >>> conf = LyDatasetConfig(year="2021", institution="usz", subsite="oropharynx")
        >>> conf.path.exists()
        True
        """
        install_loc = Path(__file__).parent.parent
        return install_loc / self.name / "data.csv"

    def get_url(self, file: str) -> str:
        """Get the URL to the dataset's directory, CSV file, or README file.

        >>> conf = LyDatasetConfig(year=2021, institution="clb", subsite="oropharynx")
        >>> conf.get_url("")
        'https://raw.githubusercontent.com/rmnldwg/lydata/main/2021-clb-oropharynx/'
        >>> conf.get_url("data.csv")
        'https://raw.githubusercontent.com/rmnldwg/lydata/main/2021-clb-oropharynx/data.csv'
        >>> conf.get_url("README.md")
        'https://raw.githubusercontent.com/rmnldwg/lydata/main/2021-clb-oropharynx/README.md'
        """
        return (
            "https://raw.githubusercontent.com/"
            f"{self.repo}/{self.ref}/"
            f"{self.year}-{self.institution}-{self.subsite}/"
        ) + file

    def get_description(self) -> str:
        """Get the description of the dataset.

        First, try to load it from the ``README.md`` file that should sit right next to
        the ``data.csv`` file. If that fails, try to look for the ``README.md`` file in
        the GitHub repository.

        >>> conf = LyDatasetConfig(year=2021, institution="clb", subsite="oropharynx")
        >>> print(conf.get_description())   # doctest: +ELLIPSIS
        # 2021 CLB Oropharynx
        ...
        """
        readme_path = self.path.with_name("README.md")
        if readme_path.exists():
            with open(readme_path, encoding="utf-8") as readme:
                return format_description(readme, short=True)

        logger.info(f"Readme not found at {readme_path}. Searching on GitHub...")
        gh = Github(auth=_get_github_auth())
        repo = gh.get_repo(self.repo)
        readme = repo.get_contents(f"{self.name}/README.md").decoded_content.decode()
        return format_description(readme, short=True)

    def load(
        self,
        skip_disk: bool = False,
        **load_kwargs,
    ) -> pd.DataFrame:
        """Load the ``data.csv`` file from disk or from GitHub.

        One can also choose to ``skip_disk``. Any keyword arguments are passed to
        :py:func:`pandas.read_csv`.

        The method will store the output of :py:meth:`~pydantic.BaseModel.model_dump`
        in the :py:attr:`~pandas.DataFrame.attrs` attribute of the returned
        :py:class:`~pandas.DataFrame`.

        >>> conf = LyDatasetConfig(year=2021, institution="clb", subsite="oropharynx")
        >>> df_from_disk = conf.load()
        >>> df_from_disk.shape
        (263, 82)
        >>> df_from_github = conf.load(skip_disk=True)
        >>> np.all(df_from_disk.fillna(0) == df_from_github.fillna(0))
        np.True_
        """
        kwargs = {"header": [0, 1, 2]}
        kwargs.update(load_kwargs)

        try:
            if skip_disk:
                logger.info(f"Skipping loading from {self.path}.")
                raise SkipDiskError
            df = pd.read_csv(self.path, **kwargs)

        except (FileNotFoundError, pd.errors.ParserError, SkipDiskError) as err:
            if isinstance(err, FileNotFoundError | pd.errors.ParserError):
                logger.info(f"Could not load from {self.path}. Trying GitHub...")

            df = pd.read_csv(self.get_url("data.csv"), **kwargs)

        df.attrs.update(self.model_dump())
        return df


def remove_subheadings(tokens: Iterable[Token], min_level: int = 1) -> list[Token]:
    """Remove anything under ``min_level`` headings.

    With this, one can truncate markdown content to e.g. to the top-level heading and
    the text that follows immediately after. Any subheadings after that will be removed.
    """
    for i, token in enumerate(tokens):
        if isinstance(token, Heading) and token.level > min_level:
            return tokens[:i]

    return list(tokens)


def format_description(
    readme: TextIOWrapper | str,
    short: bool = False,
    max_line_length: int = 60,
) -> str:
    """Get a markdown description from a file.

    Truncate the description before the first second-level heading if ``short``
    is set to ``True``.
    """
    with MarkdownRenderer(
        max_line_length=max_line_length,
        normalize_whitespace=True,
    ) as renderer:
        doc = mistletoe.Document(readme)

        if short:
            doc.children = remove_subheadings(doc.children, min_level=1)

        return renderer.render(doc)


def _available_datasets_on_disk(
    year: int | str = "*",
    institution: str = "*",
    subsite: str = "*",
) -> Generator[LyDatasetConfig, None, None]:
    year = str(year)
    search_path = Path(__file__).parent.parent

    for match in search_path.glob(f"{year}-{institution}-{subsite}"):
        if match.is_dir() and (match / "data.csv").exists():
            year, institution, subsite = match.name.split("-")
            yield LyDatasetConfig(year=year, institution=institution, subsite=subsite)


def _get_github_auth() -> Auth:
    token = os.getenv("GITHUB_TOKEN")
    user = os.getenv("GITHUB_USER")
    password = os.getenv("GITHUB_PASSWORD")

    if token:
        return Auth.Token(token)

    if user and password:
        return Auth.Login(user, password)

    raise ValueError("Neither GITHUB_TOKEN nor GITHUB_USER and GITHUB_PASSWORD set.")


def _available_datasets_on_github(
    year: int | str = "*",
    institution: str = "*",
    subsite: str = "*",
    repo: str = _repo,
    ref: str = "main",
) -> Generator[LyDatasetConfig, None, None]:
    gh = Github(auth=_get_github_auth())

    repo = gh.get_repo(repo)
    contents = repo.get_contents(path="", ref=ref)

    matches = []
    for content in contents:
        if content.type == "dir" and fnmatch.fnmatch(
            content.name, f"{year}-{institution}-{subsite}"
        ):
            matches.append(content)

    for match in matches:
        year, institution, subsite = match.name.split("-")
        yield LyDatasetConfig(
            year=year,
            institution=institution,
            subsite=subsite,
            repo=repo.full_name,
            ref=ref,
        )


def available_datasets(
    year: int | str = "*",
    institution: str = "*",
    subsite: str = "*",
    skip_disk: bool = False,
    repo: str = _repo,
    ref: str = "main",
) -> Generator[LyDatasetConfig, None, None]:
    """Generate names of available datasets.

    The arguments ``year``, ``institution``, and ``subsite`` represent glob patterns
    and all datasets matching these patterns can be iterated over using the returned
    generator.

    With ``skip_disk`` set to ``True``, the function will not look for datasets on disk.

    >>> avail_gen = available_datasets()
    >>> sorted([ds.name for ds in avail_gen])   # doctest: +NORMALIZE_WHITESPACE
    ['2021-clb-oropharynx',
     '2021-usz-oropharynx',
     '2023-clb-multisite',
     '2023-isb-multisite']
    >>> avail_gen = available_datasets(skip_disk=True)
    >>> sorted([ds.name for ds in avail_gen])   # doctest: +NORMALIZE_WHITESPACE
    ['2021-clb-oropharynx',
     '2021-usz-oropharynx',
     '2023-clb-multisite',
     '2023-isb-multisite']
    >>> avail_gen = available_datasets(
    ...     institution="hvh",
    ...     ref="6ac98d",
    ...     skip_disk=True,
    ... )
    >>> sorted([ds.get_url("") for ds in avail_gen])   # doctest: +NORMALIZE_WHITESPACE
    ['https://raw.githubusercontent.com/rmnldwg/lydata/6ac98d/2024-hvh-oropharynx/']
    """
    if not skip_disk:
        if repo != _repo or ref != "main":
            warnings.warn(
                "Parameters `repo` and `ref` are ignored, unless `skip_disk` "
                "is set to `True`."
            )
        yield from _available_datasets_on_disk(year, institution, subsite)
    else:
        yield from _available_datasets_on_github(
            year=year,
            institution=institution,
            subsite=subsite,
            repo=repo,
            ref=ref,
        )


def load_datasets(
    year: int | str = "*",
    institution: str = "*",
    subsite: str = "*",
    skip_disk: bool = False,
    repo: str = _repo,
    ref: str = "main",
    **kwargs,
) -> Generator[pd.DataFrame, None, None]:
    """Load matching datasets from the disk.

    The argument ``skip_disk`` is passed to both the :py:func:`.available_datasets`
    function to check for what can be loaded and to the :py:meth:`.LyDatasetConfig.load`
    method to decide whether to load from disk (default) or from GitHub.
    """
    dset_confs = available_datasets(
        year=year,
        institution=institution,
        subsite=subsite,
        repo=repo,
        ref=ref,
        skip_disk=skip_disk,
    )
    for dset_conf in dset_confs:
        yield dset_conf.load(skip_disk=skip_disk, **kwargs)


def load_dataset(
    year: int | str = "*",
    institution: str = "*",
    subsite: str = "*",
    skip_disk: bool = False,
    repo: str = _repo,
    ref: str = "main",
    **kwargs,
) -> pd.DataFrame:
    """Load the first matching dataset.

    ``skip_disk`` is passed to the :py:func:`load_datasets` function.

    >>> ds = load_dataset(year="2021", institution='clb', subsite='oropharynx')
    >>> ds.attrs["year"]
    2021
    >>> conf_from_ds = LyDatasetConfig(**ds.attrs)
    >>> conf_from_ds.name
    '2021-clb-oropharynx'
    """
    return next(
        load_datasets(
            year=year,
            institution=institution,
            subsite=subsite,
            skip_disk=skip_disk,
            repo=repo,
            ref=ref,
            **kwargs,
        )
    )


def join_datasets(
    year: int | str = "*",
    institution: str = "*",
    subsite: str = "*",
    skip_disk: bool = False,
    repo: str = _repo,
    ref: str = "main",
    **kwargs,
) -> pd.DataFrame:
    """Join matching datasets from the disk.

    This uses the :py:func:`.load_datasets` function to load the datasets and then
    concatenates them along the index axis.

    >>> join_datasets(year="2023").shape
    (705, 219)
    >>> join_datasets(year="2023", skip_disk=True).shape
    (705, 219)
    """
    gen = load_datasets(
        year=year,
        institution=institution,
        subsite=subsite,
        skip_disk=skip_disk,
        repo=repo,
        ref=ref,
        **kwargs,
    )
    return pd.concat(list(gen), axis="index", ignore_index=True)


def run_doctests() -> None:
    """Run the doctests."""
    import doctest

    doctest.testmod()


if __name__ == "__main__":
    run_doctests()
