r"""Generic dataset classes and auxiliary functions."""

from __future__ import annotations

from abc import ABCMeta, abstractmethod
from copy import copy as shallowcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Mapping, Optional, Union, Sequence
from typing import overload

import pandas as pd

from torch.nn import Sequential
from torch.utils.data import Dataset as TorchDataset, Subset

from ..core.config import DataclassConfig
from ..core.types import PathStr, Sample, Transform, is_namedtuple, is_path_str

from .transforms import ImageTransformConfig, image_transforms, prepend_read_image_transform


__all__ = (
    "Dataset",
    "MetaDataset",
    "ImageDataset",
    "ImageDatasetConfig",
    "GroupDataset",
    "JoinDataset",
    "read_table",
)


class Dataset(TorchDataset, metaclass=ABCMeta):
    r"""Base class of datasets with optionally on-the-fly pre-processed samples.

    This map-style dataset base class is convenient for attaching data transformations to
    a given dataset. Otherwise, datasets may also derive directly from the respective
    ``torch.utils.data`` dataset classes or simply implement the expected interfaces.

    See also: https://pytorch.org/docs/stable/data.html

    """

    def __init__(self, transform: Optional[Union[Transform, Sequence[Transform]]] = None):
        r"""Initialize dataset.

        If a dataset produces samples (i.e., a dictionary, named tuple, or custom dataclass)
        which contain fields with ``None`` values, ``collate_fn=collate_samples`` must be
        passed to ``torch.utils.data.DataLoader``. This custom collate function will ignore
        ``None`` values and pass these on to the respective batch entry. Auxiliary function
        ``prepare_batch()`` can be used to transfer the batch data retrieved by the data
        loader to the execution device.

        Args:
            transform: Data preprocessing and augmentation transforms.
                If more than one transformation is given, these will be composed
                in the given order, where the first transformation in the sequence
                is applied first. When the data samples are passed directly to
                ``torch.utils.data.DataLoader``, the transformed sample data must
                be of type ``np.ndarray``, ``torch.Tensor``, or ``None``.

        """
        super().__init__()
        if transform is not None and not isinstance(transform, Sequential):
            if not isinstance(transform, (list, tuple)):
                transform = [transform]
            transform = Sequential(*transform)
        self._transform: Optional[Sequential] = transform

    @abstractmethod
    def __len__(self) -> int:
        r"""Number of samples in dataset."""
        raise NotImplementedError

    def __getitem__(self, index: int) -> Sample:
        r"""Processed data of i-th dataset sample.

        Args:
            index: Index of dataset sample.

        Returns:
            Sample data.

        """
        sample = self.sample(index)
        if self._transform is not None:
            sample = self._transform(sample)
        return sample

    @abstractmethod
    def sample(self, index: int) -> Sample:
        r"""Data of i-th dataset sample."""
        raise NotImplementedError

    def samples(self) -> Iterable[Sample]:
        r"""Get iterable over untransformed dataset samples."""

        class SampleIterator(object):
            def __init__(self, dataset: Dataset):
                self.dataset = dataset
                self.index = -1

            def __iter__(self) -> Iterator[Sample]:
                self.index = 0
                return self

            def __next__(self) -> Sample:
                if self.index >= len(self.dataset):
                    raise StopIteration
                sample = self.dataset.sample(self.index)
                self.index += 1
                return sample

        return SampleIterator(self)

    @overload
    def transform(self) -> Sequential:
        ...

    @overload
    def transform(
        self,
        arg0: Union[Transform, Sequence[Transform], None],
        *args: Union[Transform, Sequence[Transform], None],
    ) -> Dataset:
        ...

    def transform(self, *args: Union[Transform, Sequence[Transform], None]) -> Dataset:
        r"""Get composite data preprocessing and augmentation transform, or new dataset with specified transform."""
        if not args:
            return self._transform
        return shallowcopy(self).transform_(*args)

    def transform_(self, *args: Union[Transform, Sequence[Transform], None]) -> Dataset:
        r"""Set data preprocessing and augmentation transform of this dataset."""
        transforms = []
        for arg in args:
            if arg is None:
                continue
            if isinstance(arg, (list, tuple)):
                transforms.extend(arg)
            else:
                transforms.append(arg)
        if not transforms:
            self._transform = None
        elif len(transforms) == 1 and isinstance(transforms[0], Sequential):
            self._transform = transforms[0]
        else:
            self._transform = Sequential(*transforms)
        return self


class MetaDataset(Dataset):
    r"""Dataset of file path template strings and sample meta-data given by Pandas DataFrame.

    This dataset can be used in conjunction with data reader transforms to load the data from
    configured input file paths. For example, use the ``ReadImage`` transform followed by image
    data preprocessing and augmentation functions for image data. The specified file path strings
    are Python format strings, where keywords are replaced by the respect column entries for the
    sample in the dataset index table (`pandas.DataFrame`).

    """

    def __init__(
        self,
        table: Union[Path, str, pd.DataFrame],
        paths: Optional[Mapping[str, Union[PathStr, Callable[..., PathStr]]]] = None,
        prefix: Optional[PathStr] = None,
        transform: Optional[Union[Transform, Sequence[Transform]]] = None,
        **kwargs,
    ):
        r"""Initialize dataset.

        Args:
            table: Table with sample IDs, optionally sample specific input file path template
                strings (cf. ``paths``), and additional sample meta data.
            paths: File path template strings of input data files. The format string may contain keys ``prefix``,
                when a ``prefix`` path has been specified, and ``table`` column names. The dictionary keys of this
                argument are used as sample data dictionary keys for the respective file paths. When the path value
                is a string which matches exactly the name of a ``table`` column, the value of this column is used
                without configuring a file path template string. This is useful when the input ``table`` already
                specifies the file paths for each sample. Instead of a string, the dictionary value can be a
                callable function instead, which takes the ``table`` row values as keyword arguments, and must return
                the respectively formatted input file path string. When no ``paths`` are given, the dataset samples
                only contain the meta-data from the input ``table`` columns.
            prefix: Root directory of input file paths starting with ``"{prefix}/"``.
                If ``None`` and ``table`` is a file path, it is set to the directory containing the index table.
                Otherwise, template file path strings may not contain a ``{prefix}`` key if ``None``.
            transform: Data preprocessing and augmentation transforms.
            kwargs: Additional format arguments used in addition to ``prefix`` and ``table`` column values.

        """
        if isinstance(table, (str, Path)):
            if prefix is None:
                path = Path(table).absolute()
                prefix = path.parent
            elif prefix:
                prefix = Path(prefix).absolute()
                path = prefix / Path(table)
            else:
                path = Path(table).absolute()
            table = read_table(path)
        if not isinstance(table, pd.DataFrame):
            raise TypeError(
                f"{type(self).__name__}() 'table' must be pandas.DataFrame or file path"
            )
        df: pd.DataFrame = table
        paths = {} if paths is None else dict(paths)
        if "meta" in df.columns:
            raise ValueError(
                f"{type(self).__name__} 'table' contains column with reserved name 'meta'"
            )
        if "meta" in paths:
            raise ValueError(f"{type(self).__name__} 'paths' contains reserved 'meta' key")
        prefix = Path(prefix).absolute() if prefix else None
        self.table = df
        self.paths = paths
        self.prefix = prefix
        self.kwargs = kwargs
        super().__init__(transform=transform)

    def __len__(self) -> int:
        r"""Number of samples in dataset."""
        return len(self.table)

    def row(self, index: int) -> Dict[str, Any]:
        r"""Get i-th table row values."""
        return self.table.iloc[index].to_dict()

    def sample(self, index: int) -> Dict[str, Any]:
        r"""Input file paths and/or meta-data of i-th sample in dataset."""
        meta = self.row(index)
        if not self.paths:
            return meta
        data = {}
        args = {"prefix": str(self.prefix)} if self.prefix else {}
        args.update(self.kwargs)
        args.update(meta)
        for name, path in self.paths.items():
            if callable(path):
                path = path(**args)
            elif path in meta:
                path = meta[path]
            else:
                path = path.format(**args)
            if not path:
                continue
            path = str(path)
            data[name] = path
            # Make path also available in meta-data dictionary such that even when data[name]
            # is replaced by the actual data stored at the given input file path (e.g., by a
            # ReadImage transform attached to the dataset), the file path remains available.
            meta[name] = path
        data["meta"] = meta
        return data

    def samples(self) -> Iterable[Dict[str, Any]]:
        r"""Get iterable over untransformed dataset samples."""

        class DatasetSampleIterator(object):
            def __init__(self, dataset: MetaDataset):
                self.dataset = dataset
                self.index = -1

            def __iter__(self) -> Iterator[Dict[str, Any]]:
                self.index = 0
                return self

            def __next__(self) -> Dict[str, Any]:
                if self.index >= len(self.dataset):
                    raise StopIteration
                sample = self.dataset.sample(self.index)
                self.index += 1
                return sample

        return DatasetSampleIterator(self)


@dataclass
class ImageDatasetConfig(DataclassConfig):
    r"""Configuration of image dataset."""

    table: PathStr
    images: Optional[Mapping[str, PathStr]] = None
    prefix: Optional[PathStr] = None
    transforms: Optional[Mapping[str, ImageTransformConfig]] = None

    @classmethod
    def _from_dict(
        cls, arg: Mapping[str, Any], parent: Optional[Path] = None
    ) -> ImageDatasetConfig:
        r"""Create configuration from dictionary.

        This function optionally re-organizes the dictionary entries to conform to the dataclass layout.
        It allows the image data transforms to be specified as separate "transforms" entry for each image.
        In this case, the image file path template string must given by the "path" dictionary entry.
        Additionally, a "read" image transform is added when a "dtype" or "device" is specified on which
        the image data is loaded and preprocessed can also be specified alongside the file "path".
        Any image "transforms" specified at the top-level are applied after any "transforms" specified
        underneath the "images" key.

        """
        arg = dict(arg)
        images = arg.pop("images") or {}
        transforms = arg.pop("transforms") or {}
        image_paths = {}
        for name, value in images.items():
            dtype = None
            device = None
            image_transforms = []
            if isinstance(value, Mapping):
                if "path" not in value:
                    raise ValueError(
                        f"{cls.__name__}.from_dict() 'images' key '{name}' dict must contain 'path' entry"
                    )
                path = value["path"]
                dtype = value.get("dtype", dtype)
                device = value.get("device", device)
                image_transforms = value.get("transforms", image_transforms)
                if not isinstance(image_transforms, Sequence):
                    raise TypeError(
                        f"{cls.__name__}.from_dict() image 'transforms' value must be Sequence"
                    )
            elif is_path_str(value):
                path = Path(value).as_posix()
            else:
                raise ValueError(
                    f"{cls.__name__}.from_dict() 'images' key '{name}' must be PathStr or dict with 'path' entry"
                )
            if name in transforms:
                item_transforms = transforms[name]
                if not isinstance(item_transforms, Sequence):
                    raise TypeError(
                        f"{cls.__name__}.from_dict() 'transforms' dict value must be Sequence"
                    )
                item_transforms = list(item_transforms)
            else:
                item_transforms = []
            image_transforms = image_transforms + item_transforms
            if dtype or device:
                image_transforms = prepend_read_image_transform(
                    image_transforms, dtype=dtype, device=device
                )
            transforms[name] = image_transforms
            image_paths[name] = path
        if image_paths:
            arg["images"] = image_paths
        if transforms:
            arg["transforms"] = transforms
        super()._from_dict(arg, parent)


class ImageDataset(MetaDataset):
    r"""Configurable image dataset."""

    @classmethod
    def from_config(cls, config: ImageDatasetConfig) -> ImageDataset:
        transforms = []
        for image_name in config.images:
            image_transforms_config = config.transforms.get(image_name, [])
            image_transforms_config = prepend_read_image_transform(image_transforms_config)
            item_transforms = image_transforms(image_transforms_config, key=image_name)
            transforms.extend(item_transforms)
        return cls(config.table, paths=config.images, prefix=config.prefix, transforms=transforms)


class GroupDataset(TorchDataset):
    r"""Group samples in dataset."""

    def __init__(
        self,
        dataset: MetaDataset,
        groupby: Union[Sequence[str], str],
        sortby: Optional[Union[Sequence[str], str]] = None,
        ascending: bool = True,
    ) -> None:
        super().__init__()
        indices = []
        df = dataset.table
        if sortby:
            df = df.sort_values(sortby, ascending=ascending)
        groups = df.groupby(groupby)
        for _, group in groups:
            assert isinstance(group, pd.DataFrame)
            ilocs = [row[0] for row in group.itertuples(index=True)]
            indices.append(ilocs)
        self.dataset = dataset
        self.indices = indices

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, index: int) -> Subset[Dict[str, Any]]:
        indices = self.indices[index]
        return Subset(self.dataset, indices)


class JoinDataset(Dataset):
    r"""Join dict entries from one or more datasets in a single dict."""

    def __init__(self, datasets: Iterable[Dataset]) -> None:
        super().__init__()
        datasets = list(datasets)
        if not all(len(dataset) == len(datasets[0]) for dataset in datasets):
            raise ValueError("JoinDataset() 'datasets' must have the same size")
        self.datasets = datasets

    def __len__(self) -> int:
        datasets = self.datasets
        return len(datasets[0]) if datasets else 0

    def sample(self, index: int) -> Sample:
        sample = {}
        for i, dataset in enumerate(self.datasets):
            data = dataset[index]
            if not isinstance(data, dict):
                if is_namedtuple(data):
                    data = data._asdict()
                else:
                    data = {str(i): data}
            for key, value in data.items():
                current = sample.get(key, None)
                if current is not None and current != value:
                    raise ValueError("JoinDataset() encountered ambiguous duplicate key '{key}'")
                sample[key] = value
        return sample


def read_table(path: PathStr) -> pd.DataFrame:
    r"""Read dataset index table."""
    path = Path(path).absolute()
    if path.suffix.lower() == ".h5":
        return pd.read_hdf(path)
    if path.suffix.lower() == ".tsv":
        return pd.read_csv(path, comment="#", skip_blank_lines=True, delimiter="\t")
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, comment="#", skip_blank_lines=True)
    raise NotImplementedError(f"read_table() does not support {path.suffix} file format")
