# Copyright 2021 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import nltk
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import seaborn as sns
import statistics
import utils
import utils.dataset_utils as ds_utils
from data_measurements.embeddings.embeddings import Embeddings
from data_measurements.labels import labels
from data_measurements.npmi.npmi import nPMI
from data_measurements.text_duplicates import text_duplicates as td
from data_measurements.zipf import zipf
from datasets import load_from_disk, load_metric
from nltk.corpus import stopwords
from os import mkdir, getenv
from os.path import exists, isdir
from os.path import join as pjoin
from pathlib import Path
from sklearn.feature_extraction.text import CountVectorizer
from utils.dataset_utils import (CNT, EMBEDDING_FIELD, LENGTH_FIELD,
                                 OUR_TEXT_FIELD, PERPLEXITY_FIELD, PROP,
                                 TEXT_NAN_CNT, TOKENIZED_FIELD, TOT_OPEN_WORDS,
                                 TOT_WORDS, VOCAB, WORD)

logs = utils.prepare_logging(__file__)

# TODO: Read this in depending on chosen language / expand beyond english
nltk.download("stopwords")
_CLOSED_CLASS = (
        stopwords.words("english")
        + [
            "t",
            "n",
            "ll",
            "d",
            "wasn",
            "weren",
            "won",
            "aren",
            "wouldn",
            "shouldn",
            "didn",
            "don",
            "hasn",
            "ain",
            "couldn",
            "doesn",
            "hadn",
            "haven",
            "isn",
            "mightn",
            "mustn",
            "needn",
            "shan",
            "would",
            "could",
            "dont",
            "u",
        ]
        + [str(i) for i in range(0, 21)]
)
_IDENTITY_TERMS = [
    "man",
    "woman",
    "non-binary",
    "gay",
    "lesbian",
    "queer",
    "trans",
    "straight",
    "cis",
    "she",
    "her",
    "hers",
    "he",
    "him",
    "his",
    "they",
    "them",
    "their",
    "theirs",
    "himself",
    "herself",
]
# treating inf values as NaN as well
pd.set_option("use_inf_as_na", True)

MIN_VOCAB_COUNT = 10
_TREE_DEPTH = 12
_TREE_MIN_NODES = 250
# as long as we're using sklearn - already pushing the resources
_MAX_CLUSTER_EXAMPLES = 5000
_NUM_VOCAB_BATCHES = 2000
_TOP_N = 100
_CVEC = CountVectorizer(token_pattern="(?u)\\b\\w+\\b", lowercase=True)

_PERPLEXITY = load_metric("perplexity")


class DatasetStatisticsCacheClass:

    def __init__(
            self,
            dset_name,
            dset_config,
            split_name,
            text_field,
            label_field,
            label_names,
            cache_dir="cache_dir",
            dataset_cache_dir=None,
            use_cache=False,
            save=True,
    ):

        ### What are we analyzing?
        # name of the Hugging Face dataset
        self.dset_name = dset_name
        # original HuggingFace dataset
        self.dset = None
        # name of the dataset config
        self.dset_config = dset_config
        # name of the split to analyze
        self.split_name = split_name
        # which text/feature fields are we analysing?
        self.text_field = text_field

        ## Label variables
        # which label fields are we analysing?
        self.label_field = label_field
        # what are the names of the classes?
        self.label_names = label_names
        # where are they being cached?
        self.label_files = {}
        # label pie chart used in the UI
        self.fig_labels = None
        # results
        self.label_results = None

        ## Caching
        if not dataset_cache_dir:
            _, self.dataset_cache_dir = ds_utils.get_cache_dir_naming(cache_dir,
                                                                      dset_name,
                                                                      dset_config,
                                                                      split_name,
                                                                      text_field)
        else:
            self.dataset_cache_dir = dataset_cache_dir

        # Use stored data if there; otherwise calculate afresh
        self.use_cache = use_cache
        # Save newly calculated results.
        self.save = save

        # HF dataset with all of the self.text_field instances in self.dset
        self.text_dset = None
        self.dset_peek = None
        # Tokenized text
        self.tokenized_df = None

        ## Zipf
        # Save zipf fig so it doesn't need to be recreated.
        self.zipf_fig = None
        # Zipf object
        self.z = None

        ## Vocabulary
        # Vocabulary with word counts in the dataset
        self.vocab_counts_df = None
        # Vocabulary filtered to remove stopwords
        self.vocab_counts_filtered_df = None
        self.sorted_top_vocab_df = None

        # Text Duplicates
        self.duplicates_results = None
        self.duplicates_files = {}
        self.dups_frac = 0
        self.dups_dict = {}

        ## Perplexity
        self.perplexities_df = None

        ## Lengths
        self.avg_length = None
        self.std_length = None
        self.length_stats_dict = None
        self.length_df = None
        self.fig_tok_length = None
        self.num_uniq_lengths = 0

        ## "General" stats
        self.general_stats_dict = {}
        self.total_words = 0
        self.total_open_words = 0
        # Number of NaN values (NOT empty strings)
        self.text_nan_count = 0

        # nPMI
        # Holds a nPMIStatisticsCacheClass object
        self.npmi_stats = None
        # The minimum amount of times a word should occur to be included in
        # word-count-based calculations (currently just relevant to nPMI)
        self.min_vocab_count = MIN_VOCAB_COUNT
        self.cvec = _CVEC

        self.hf_dset_cache_dir = pjoin(self.dataset_cache_dir, "base_dset")
        self.tokenized_df_fid = pjoin(self.dataset_cache_dir, "tokenized_df.feather")

        self.text_dset_fid = pjoin(self.dataset_cache_dir, "text_dset")
        self.dset_peek_json_fid = pjoin(self.dataset_cache_dir, "dset_peek.json")

        ## Length cache files
        self.length_df_fid = pjoin(self.dataset_cache_dir, "length_df.feather")
        self.length_stats_json_fid = pjoin(self.dataset_cache_dir, "length_stats.json")

        self.vocab_counts_df_fid = pjoin(self.dataset_cache_dir,
                                         "vocab_counts.feather")
        self.dup_counts_df_fid = pjoin(self.dataset_cache_dir, "dup_counts_df.feather")
        self.perplexities_df_fid = pjoin(self.dataset_cache_dir,
                                         "perplexities_df.feather")
        self.fig_tok_length_fid = pjoin(self.dataset_cache_dir, "fig_tok_length.png")

        ## General text stats
        self.general_stats_json_fid = pjoin(self.dataset_cache_dir,
                                            "general_stats_dict.json")
        # Needed for UI
        self.sorted_top_vocab_df_fid = pjoin(
            self.dataset_cache_dir, "sorted_top_vocab.feather"
        )
        # Set the HuggingFace dataset object with the given arguments.
        self.dset = self.get_dataset()


    def get_dataset(self):
        """
        Gets the HuggingFace Dataset object.
        First tries to use the given cache directory if specified;
        otherwise saves to the given cache directory if specified.
        """
        dset = ds_utils.load_truncated_dataset(self.dset_name, self.dset_config,
                                               self.split_name,
                                               cache_dir=self.hf_dset_cache_dir,
                                               save=self.save)
        return dset


    def load_or_prepare_general_stats(self, load_only=False):
        """
        Content for expander_general_stats widget.
        Provides statistics for total words, total open words,
        the sorted top vocab, the NaN count, and the duplicate count.
        Args:

        Returns:

        """
        # General statistics
        # For the general statistics, text duplicates are not saved in their
        # own files, but rather just the text duplicate fraction is saved in the
        # "general" file. We therefore set save=False for
        # the text duplicate files in this case.
        # Similarly, we don't get the full list of duplicates
        # in general stats, so set list_duplicates to False
        self.load_or_prepare_text_duplicates(load_only=load_only, save=False,
                                             list_duplicates=False)
        logs.info("Duplicates results:")
        logs.info(self.duplicates_results)
        self.general_stats_dict.update(self.duplicates_results)
        # TODO: Tighten the rest of this similar to text_duplicates.
        if (
                self.use_cache
                and exists(self.general_stats_json_fid)
                and exists(self.sorted_top_vocab_df_fid)
        ):
            logs.info("Loading cached general stats")
            self.load_general_stats()
        elif not load_only:
            logs.info("Preparing general stats")
            self.prepare_general_stats()
            if self.save:
                ds_utils.write_df(self.sorted_top_vocab_df,
                               self.sorted_top_vocab_df_fid)
                ds_utils.write_json(self.general_stats_dict,
                                 self.general_stats_json_fid)

    def load_or_prepare_text_lengths(self, load_only=False):
        """
        The text length widget relies on this function, which provides
        a figure of the text lengths, some text length statistics, and
        a text length dataframe to peruse.
        Args:
            save:
        Returns:

        """
        # Text length figure
        if self.use_cache and exists(self.fig_tok_length_fid):
            self.fig_tok_length_png = mpimg.imread(self.fig_tok_length_fid)
        elif not load_only:
            self.prepare_fig_text_lengths()
            if self.save:
                self.fig_tok_length.savefig(self.fig_tok_length_fid)
        # Text length dataframe
        if self.use_cache and exists(self.length_df_fid):
            self.length_df = ds_utils.read_df(self.length_df_fid)
        elif not load_only:
            self.prepare_length_df()
            if self.save:
                ds_utils.write_df(self.length_df, self.length_df_fid)

        # Text length stats.
        if self.use_cache and exists(self.length_stats_json_fid):
            with open(self.length_stats_json_fid, "r") as f:
                self.length_stats_dict = json.load(f)
            self.avg_length = self.length_stats_dict["avg length"]
            self.std_length = self.length_stats_dict["std length"]
            self.num_uniq_lengths = self.length_stats_dict["num lengths"]
        elif not load_only:
            self.prepare_text_length_stats()
            if self.save:
                ds_utils.write_json(self.length_stats_dict,
                                 self.length_stats_json_fid)

    def prepare_length_df(self):
        self.tokenized_df[LENGTH_FIELD] = self.tokenized_df[
            TOKENIZED_FIELD].apply(
            len
        )
        self.length_df = self.tokenized_df[
            [LENGTH_FIELD, OUR_TEXT_FIELD]
        ].sort_values(by=[LENGTH_FIELD], ascending=True)

    def prepare_text_length_stats(self):
        if (
                LENGTH_FIELD not in self.tokenized_df.columns
                or self.length_df is None
        ):
            self.prepare_length_df()
        avg_length = sum(self.tokenized_df[LENGTH_FIELD]) / len(
            self.tokenized_df[LENGTH_FIELD]
        )
        self.avg_length = round(avg_length, 1)
        std_length = statistics.stdev(self.tokenized_df[LENGTH_FIELD])
        self.std_length = round(std_length, 1)
        self.num_uniq_lengths = len(self.length_df["length"].unique())
        self.length_stats_dict = {
            "avg length": self.avg_length,
            "std length": self.std_length,
            "num lengths": self.num_uniq_lengths,
        }

    def prepare_fig_text_lengths(self):
        if LENGTH_FIELD not in self.tokenized_df.columns:
            self.prepare_length_df()
        self.fig_tok_length = make_fig_lengths(self.tokenized_df,
                                               LENGTH_FIELD)

    ## Labels functions
    def load_or_prepare_labels(self, load_only=False):
        """Uses a generic Labels class, with attributes specific to this
        project as input.
        Computes results for each label column,
        or else uses what's available in the cache.
        Currently supports Datasets with just one label column.
        """
        label_obj = labels.DMTHelper(self, load_only=load_only, save=self.save)
        label_obj.run_DMT_processing()
        self.fig_labels = label_obj.fig_labels
        self.label_results = label_obj.label_results
        self.label_files = label_obj.get_label_filenames()

    # Get vocab with word counts
    def load_or_prepare_vocab(self, load_only=False):
        """
        Calculates the vocabulary count from the tokenized text.
        The resulting dataframes may be used in nPMI calculations, zipf, etc.
        :param
        :return:
        """
        if self.use_cache and exists(self.vocab_counts_df_fid):
            logs.info("Reading vocab from cache")
            self.load_vocab()
            self.vocab_counts_filtered_df = filter_vocab(self.vocab_counts_df)
        elif not load_only:
            # Building the vocabulary starts with tokenizing.
            self.load_or_prepare_tokenized_df(load_only=False)
            logs.info("Calculating vocab afresh")
            word_count_df = count_vocab_frequencies(self.tokenized_df)
            logs.info("Making dfs with proportion.")
            self.vocab_counts_df = calc_p_word(word_count_df)
            self.vocab_counts_filtered_df = filter_vocab(self.vocab_counts_df)
            if self.save:
                logs.info("Writing out.")
                ds_utils.write_df(self.vocab_counts_df, self.vocab_counts_df_fid)
        logs.info("unfiltered vocab")
        logs.info(self.vocab_counts_df)
        logs.info("filtered vocab")
        logs.info(self.vocab_counts_filtered_df)

    def load_vocab(self):
        with open(self.vocab_counts_df_fid, "rb") as f:
            self.vocab_counts_df = ds_utils.read_df(f)
        # Handling for changes in how the index is saved.
        self.vocab_counts_df = _set_idx_col_names(self.vocab_counts_df)

    def load_or_prepare_text_duplicates(self, load_only=False, save=True, list_duplicates=True):
        """Uses a text duplicates library, which
        returns strings with their counts, fraction of data that is duplicated,
        or else uses what's available in the cache.
        """
        dups_obj = td.DMTHelper(self, load_only=load_only, save=save)
        dups_obj.run_DMT_processing(list_duplicates=list_duplicates)
        self.duplicates_results = dups_obj.duplicates_results
        self.dups_frac = self.duplicates_results[td.DUPS_FRAC]
        if list_duplicates and td.DUPS_DICT in self.duplicates_results:
            self.dups_dict = self.duplicates_results[td.DUPS_DICT]
        self.duplicates_files = dups_obj.get_duplicates_filenames()


    def load_or_prepare_text_perplexities(self, load_only=False):
        if self.use_cache and exists(self.perplexities_df_fid):
            with open(self.perplexities_df_fid, "rb") as f:
                self.perplexities_df = ds_utils.read_df(f)
        elif not load_only:
            self.prepare_text_perplexities()
            if self.save:
                ds_utils.write_df(self.perplexities_df,
                               self.perplexities_df_fid)

    def load_general_stats(self):
        self.general_stats_dict = json.load(
            open(self.general_stats_json_fid, encoding="utf-8")
        )
        with open(self.sorted_top_vocab_df_fid, "rb") as f:
            self.sorted_top_vocab_df = ds_utils.read_df(f)
        self.text_nan_count = self.general_stats_dict[TEXT_NAN_CNT]
        self.dups_frac = self.general_stats_dict[td.DUPS_FRAC]
        self.total_words = self.general_stats_dict[TOT_WORDS]
        self.total_open_words = self.general_stats_dict[TOT_OPEN_WORDS]

    def prepare_general_stats(self):
        if self.tokenized_df is None:
            logs.warning("Tokenized dataset not yet loaded; doing so.")
            self.load_or_prepare_tokenized_df()
        if self.vocab_counts_df is None:
            logs.warning("Vocab not yet loaded; doing so.")
            self.load_or_prepare_vocab()
        self.sorted_top_vocab_df = self.vocab_counts_filtered_df.sort_values(
            "count", ascending=False
        ).head(_TOP_N)
        self.total_words = len(self.vocab_counts_df)
        self.total_open_words = len(self.vocab_counts_filtered_df)
        self.text_nan_count = int(self.tokenized_df.isnull().sum().sum())
        self.general_stats_dict = {
            TOT_WORDS: self.total_words,
            TOT_OPEN_WORDS: self.total_open_words,
            TEXT_NAN_CNT: self.text_nan_count,
            td.DUPS_FRAC: self.dups_frac
        }

    def prepare_text_perplexities(self):
        if self.text_dset is None:
            self.load_or_prepare_text_dset()
        results = _PERPLEXITY.compute(
            input_texts=self.text_dset[OUR_TEXT_FIELD], model_id='gpt2')
        perplexities = {PERPLEXITY_FIELD: results["perplexities"],
                        OUR_TEXT_FIELD: self.text_dset[OUR_TEXT_FIELD]}
        self.perplexities_df = pd.DataFrame(perplexities).sort_values(
            by=PERPLEXITY_FIELD, ascending=False)

    def load_or_prepare_dataset(self, load_only=False):
        """
        Prepares the HF datasets and data frames containing the untokenized and
        tokenized text as well as the label values.
        self.tokenized_df is used further for calculating text lengths,
        word counts, etc.
        Args:
            save: Store the calculated data to disk.

        Returns:

        """
        if not self.dset:
            self.prepare_base_dataset(load_only=load_only)
        logs.info("Doing text dset.")
        self.load_or_prepare_text_dset(load_only=load_only)

    # TODO: Are we not using this anymore?
    def load_or_prepare_dset_peek(self, load_only=False):
        if self.use_cache and exists(self.dset_peek_json_fid):
            with open(self.dset_peek_json_fid, "r") as f:
                self.dset_peek = json.load(f)["dset peek"]
        elif not load_only:
            if self.dset is None:
                self.get_base_dataset()
            self.dset_peek = self.dset[:100]
            if self.save:
                ds_utils.write_json({"dset peek": self.dset_peek},
                                 self.dset_peek_json_fid)

    def load_or_prepare_tokenized_df(self, load_only=False):
        if self.use_cache and exists(self.tokenized_df_fid):
            self.tokenized_df = ds_utils.read_df(self.tokenized_df_fid)
        elif not load_only:
            # tokenize all text instances
            self.tokenized_df = self.do_tokenization()
            if self.save:
                logs.warning("Saving tokenized dataset to disk")
                # save tokenized text
                ds_utils.write_df(self.tokenized_df, self.tokenized_df_fid)

    def load_or_prepare_text_dset(self, load_only=False):
        if self.use_cache and exists(self.text_dset_fid):
            # load extracted text
            self.text_dset = load_from_disk(self.text_dset_fid)
            logs.warning("Loaded dataset from disk")
            logs.warning(self.text_dset)
        # ...Or load it from the server and store it anew
        elif not load_only:
            self.prepare_text_dset()
            if self.save:
                # save extracted text instances
                logs.warning("Saving dataset to disk")
                self.text_dset.save_to_disk(self.text_dset_fid)

    def prepare_text_dset(self):
        self.get_base_dataset()
        logs.warning(self.dset)
        # extract all text instances
        self.text_dset = self.dset.map(
            lambda examples: ds_utils.extract_field(
                examples, self.text_field, OUR_TEXT_FIELD
            ),
            batched=True,
            remove_columns=list(self.dset.features),
        )

    def do_tokenization(self):
        """
        Tokenizes the dataset
        :return:
        """
        if self.text_dset is None:
            self.load_or_prepare_text_dset()
        sent_tokenizer = self.cvec.build_tokenizer()

        def tokenize_batch(examples):
            # TODO: lowercase should be an option
            res = {
                TOKENIZED_FIELD: [
                    tuple(sent_tokenizer(text.lower()))
                    for text in examples[OUR_TEXT_FIELD]
                ]
            }
            res[LENGTH_FIELD] = [len(tok_text) for tok_text in
                                 res[TOKENIZED_FIELD]]
            return res

        tokenized_dset = self.text_dset.map(
            tokenize_batch,
            batched=True,
            # remove_columns=[OUR_TEXT_FIELD], keep around to print
        )
        tokenized_df = pd.DataFrame(tokenized_dset)
        return tokenized_df

    def load_or_prepare_npmi(self, load_only=False):
        self.npmi_stats = nPMIStatisticsCacheClass(self, load_only=load_only,
                                                   use_cache=self.use_cache)
        self.npmi_stats.load_or_prepare_npmi_terms()

    def load_or_prepare_zipf(self, load_only=False):
        zipf_json_fid, zipf_fig_json_fid, zipf_fig_html_fid = zipf.get_zipf_fids(
            self.dataset_cache_dir)
        if self.use_cache and exists(zipf_json_fid):
            # Zipf statistics
            # Read Zipf statistics: Alpha, p-value, etc.
            with open(zipf_json_fid, "r") as f:
                zipf_dict = json.load(f)
            self.z = zipf.Zipf(self.vocab_counts_df)
            self.z.load(zipf_dict)
            # Zipf figure
            if exists(zipf_fig_json_fid):
                self.zipf_fig = ds_utils.read_plotly(zipf_fig_json_fid)
            elif not load_only:
                self.zipf_fig = zipf.make_zipf_fig(self.z)
                if self.save:
                    ds_utils.write_plotly(self.zipf_fig)
        elif not load_only:
            self.prepare_zipf()
            if self.save:
                zipf_dict = self.z.get_zipf_dict()
                ds_utils.write_json(zipf_dict, zipf_json_fid)
                ds_utils.write_plotly(self.zipf_fig, zipf_fig_json_fid)
                self.zipf_fig.write_html(zipf_fig_html_fid)

    def prepare_zipf(self):
        # Calculate zipf from scratch
        # TODO: Does z even need to be self?
        self.z = zipf.Zipf(self.vocab_counts_df)
        self.z.calc_fit()
        self.zipf_fig = zipf.make_zipf_fig(self.z)

def _set_idx_col_names(input_vocab_df):
    if input_vocab_df.index.name != VOCAB and VOCAB in input_vocab_df.columns:
        input_vocab_df = input_vocab_df.set_index([VOCAB])
        input_vocab_df[VOCAB] = input_vocab_df.index
    return input_vocab_df


def _set_idx_cols_from_cache(csv_df, subgroup=None, calc_str=None):
    """
    Helps make sure all of the read-in files can be accessed within code
    via standardized indices and column names.
    :param csv_df:
    :param subgroup:
    :param calc_str:
    :return:
    """
    # The csv saves with this column instead of the index, so that's weird.
    if "Unnamed: 0" in csv_df.columns:
        csv_df = csv_df.set_index("Unnamed: 0")
        csv_df.index.name = WORD
    elif WORD in csv_df.columns:
        csv_df = csv_df.set_index(WORD)
        csv_df.index.name = WORD
    elif VOCAB in csv_df.columns:
        csv_df = csv_df.set_index(VOCAB)
        csv_df.index.name = WORD
    if subgroup and calc_str:
        csv_df.columns = [subgroup + "-" + calc_str]
    elif subgroup:
        csv_df.columns = [subgroup]
    elif calc_str:
        csv_df.columns = [calc_str]
    return csv_df


class nPMIStatisticsCacheClass:
    """ "Class to interface between the app and the nPMI class
    by calling the nPMI class with the user's selections."""

    def __init__(self, dataset_stats, load_only=False, use_cache=False):
        self.dstats = dataset_stats
        self.pmi_dataset_cache_dir = pjoin(self.dstats.dataset_cache_dir, "pmi_files")
        if not isdir(self.pmi_dataset_cache_dir):
            logs.warning(
                "Creating pmi cache directory %s." % self.pmi_dataset_cache_dir)
            # We need to preprocess everything.
            mkdir(self.pmi_dataset_cache_dir)
        self.joint_npmi_df_dict = {}
        # TODO: Users ideally can type in whatever words they want.
        self.termlist = _IDENTITY_TERMS
        # termlist terms that are available more than MIN_VOCAB_COUNT times
        self.available_terms = _IDENTITY_TERMS
        logs.info(self.termlist)
        self.use_cache = use_cache
        self.load_only = load_only
        # TODO: Let users specify
        self.open_class_only = True
        self.min_vocab_count = self.dstats.min_vocab_count
        self.subgroup_files = {}
        self.npmi_terms_fid = pjoin(self.dstats.dataset_cache_dir, "npmi_terms.json")

    def load_or_prepare_npmi_terms(self):
        """
        Figures out what identity terms the user can select, based on whether
        they occur more than self.min_vocab_count times
        :return: Identity terms occurring at least self.min_vocab_count times.
        """
        # TODO: Add the user's ability to select subgroups.
        # TODO: Make min_vocab_count here value selectable by the user.
        logs.info("Looking for cached terms in " % self.npmi_terms_fid)
        if (
                self.use_cache
                and exists(self.npmi_terms_fid)
                and json.load(open(self.npmi_terms_fid))[
            "available terms"] != []
        ):
            available_terms = json.load(open(self.npmi_terms_fid))[
                "available terms"]
        elif not self.load_only:
            true_false = [
                term in self.dstats.vocab_counts_df.index for term in
                self.termlist
            ]
            word_list_tmp = [x for x, y in zip(self.termlist, true_false) if y]
            true_false_counts = [
                self.dstats.vocab_counts_df.loc[
                    word, CNT] >= self.min_vocab_count
                for word in word_list_tmp
            ]
            available_terms = [
                word for word, y in zip(word_list_tmp, true_false_counts) if y
            ]
            logs.info(available_terms)
            with open(self.npmi_terms_fid, "w+") as f:
                json.dump({"available terms": available_terms}, f)
        self.available_terms = available_terms
        return available_terms

    def load_or_prepare_joint_npmi(self, subgroup_pair):
        """
        Run on-the fly, while the app is already open,
        as it depends on the subgroup terms that the user chooses
        :param subgroup_pair:
        :return:
        """
        # Canonical ordering for subgroup_list
        subgroup_pair = sorted(subgroup_pair)
        subgroup1 = subgroup_pair[0]
        subgroup2 = subgroup_pair[1]
        subgroups_str = "-".join(subgroup_pair)
        if not isdir(self.pmi_dataset_cache_dir):
            logs.warning("Creating cache")
            # We need to preprocess everything.
            # This should eventually all go into a prepare_dataset CLI
            mkdir(self.pmi_dataset_cache_dir)
        joint_npmi_fid = pjoin(self.pmi_dataset_cache_dir, subgroups_str + "_npmi.csv")
        subgroup_files = define_subgroup_files(subgroup_pair,
                                               self.pmi_dataset_cache_dir)
        # Defines the filenames for the cache files from the selected subgroups.
        # Get as much precomputed data as we can.
        if self.use_cache and exists(joint_npmi_fid):
            # When everything is already computed for the selected subgroups.
            logs.info("Loading cached joint npmi")
            joint_npmi_df = self.load_joint_npmi_df(joint_npmi_fid)
            npmi_display_cols = [
                "npmi-bias",
                subgroup1 + "-npmi",
                subgroup2 + "-npmi",
                subgroup1 + "-count",
                subgroup2 + "-count",
            ]
            joint_npmi_df = joint_npmi_df[npmi_display_cols]
            # When maybe some things have been computed for the selected subgroups.
        else:
            logs.debug("Preparing new joint npmi")
            joint_npmi_df, subgroup_dict = self.prepare_joint_npmi_df(
                subgroup_pair, subgroup_files
            )
            # Cache new results
            logs.debug("Writing out.")
            for subgroup in subgroup_pair:
                write_subgroup_npmi_data(subgroup, subgroup_dict,
                                         subgroup_files)
            with open(joint_npmi_fid, "w+") as f:
                joint_npmi_df.to_csv(f)
        logs.debug("The joint npmi df is")
        logs.debug(joint_npmi_df)
        return joint_npmi_df

    @staticmethod
    def load_joint_npmi_df(joint_npmi_fid):
        """
        Reads in a saved dataframe with all of the paired results.
        :param joint_npmi_fid:
        :return: paired results
        """
        with open(joint_npmi_fid, "rb") as f:
            joint_npmi_df = pd.read_csv(f)
        joint_npmi_df = _set_idx_cols_from_cache(joint_npmi_df)
        return joint_npmi_df.dropna()

    def prepare_joint_npmi_df(self, subgroup_pair, subgroup_files):
        """
        Computs the npmi bias based on the given subgroups.
        Handles cases where some of the selected subgroups have cached nPMI
        computations, but other's don't, computing everything afresh if there
        are not cached files.
        :param subgroup_pair:
        :return: Dataframe with nPMI for the words, nPMI bias between the words.
        """
        subgroup_dict = {}
        # When npmi is computed for some (but not all) of subgroup_list
        for subgroup in subgroup_pair:
            logs.debug("Load or failing...")
            # When subgroup npmi has been computed in a prior session.
            cached_results = self.load_or_fail_cached_npmi_scores(
                subgroup, subgroup_files[subgroup]
            )
            # If the function did not return False and we did find it, use.
            if cached_results:
                # FYI: subgroup_cooc_df, subgroup_pmi_df, subgroup_npmi_df = cached_results
                # Holds the previous sessions' data for use in this session.
                subgroup_dict[subgroup] = cached_results
        logs.debug("Calculating for subgroup list")
        joint_npmi_df, subgroup_dict = self.do_npmi(subgroup_pair,
                                                    subgroup_dict)
        return joint_npmi_df.dropna(), subgroup_dict

    # TODO: Update pairwise assumption
    def do_npmi(self, subgroup_pair, subgroup_dict):
        """
        Calculates nPMI for given identity terms and the nPMI bias between.
        :param subgroup_pair: List of identity terms to calculate the bias for
        :return: Subset of data for the UI
        :return: Selected identity term's co-occurrence counts with
                 other words, pmi per word, and nPMI per word.
        """
        logs.debug("Initializing npmi class")
        npmi_obj = self.set_npmi_obj()
        # Canonical ordering used
        subgroup_pair = tuple(sorted(subgroup_pair))
        # Calculating nPMI statistics
        for subgroup in subgroup_pair:
            # If the subgroup data is already computed, grab it.
            # TODO: Should we set idx and column names similarly to how we set them for cached files?
            if subgroup not in subgroup_dict:
                logs.info("Calculating npmi statistics for %s" % subgroup)
                vocab_cooc_df, pmi_df, npmi_df = npmi_obj.calc_metrics(subgroup)
                # Store the nPMI information for the current subgroups
                subgroup_dict[subgroup] = (vocab_cooc_df, pmi_df, npmi_df)
        # Pair the subgroups together, indexed by all words that
        # co-occur between them.
        logs.debug("Computing pairwise npmi bias")
        paired_results = npmi_obj.calc_paired_metrics(subgroup_pair,
                                                      subgroup_dict)
        UI_results = make_npmi_fig(paired_results, subgroup_pair)
        return UI_results, subgroup_dict

    def set_npmi_obj(self):
        """
        Initializes the nPMI class with the given words and tokenized sentences.
        :return:
        """
        # TODO(meg): Incorporate this from evaluate library.
        # npmi_obj = evaluate.load('npmi', module_type='measurement').compute(subgroup, vocab_counts_df = self.dstats.vocab_counts_df, tokenized_counts_df=self.dstats.tokenized_df)
        npmi_obj = nPMI(self.dstats.vocab_counts_df, self.dstats.tokenized_df)
        return npmi_obj

    @staticmethod
    def load_or_fail_cached_npmi_scores(subgroup, subgroup_fids):
        """
        Reads cached scores from the specified subgroup files
        :param subgroup: string of the selected identity term
        :return:
        """
        # TODO: Ordering of npmi, pmi, vocab triple should be consistent
        subgroup_npmi_fid, subgroup_pmi_fid, subgroup_cooc_fid = subgroup_fids
        if (
                exists(subgroup_npmi_fid)
                and exists(subgroup_pmi_fid)
                and exists(subgroup_cooc_fid)
        ):
            logs.debug("Reading in pmi data....")
            with open(subgroup_cooc_fid, "rb") as f:
                subgroup_cooc_df = pd.read_csv(f)
            logs.debug("pmi")
            with open(subgroup_pmi_fid, "rb") as f:
                subgroup_pmi_df = pd.read_csv(f)
            logs.debug("npmi")
            with open(subgroup_npmi_fid, "rb") as f:
                subgroup_npmi_df = pd.read_csv(f)
            subgroup_cooc_df = _set_idx_cols_from_cache(
                subgroup_cooc_df, subgroup, "count"
            )
            subgroup_pmi_df = _set_idx_cols_from_cache(
                subgroup_pmi_df, subgroup, "pmi"
            )
            subgroup_npmi_df = _set_idx_cols_from_cache(
                subgroup_npmi_df, subgroup, "npmi"
            )
            return subgroup_cooc_df, subgroup_pmi_df, subgroup_npmi_df
        return False

    def get_available_terms(self):
        return self.load_or_prepare_npmi_terms()


def dummy(doc):
    return doc


def count_vocab_frequencies(tokenized_df):
    """
    Based on an input pandas DataFrame with a 'text' column,
    this function will count the occurrences of all words.
    :return: [num_words x num_sentences] DataFrame with the rows corresponding to the
    different vocabulary words and the column to the presence (0 or 1) of that word.
    """

    cvec = CountVectorizer(
        tokenizer=dummy,
        preprocessor=dummy,
    )
    # We do this to calculate per-word statistics
    # Fast calculation of single word counts
    logs.info(
        "Fitting dummy tokenization to make matrix using the previous tokenization"
    )
    cvec.fit(tokenized_df[TOKENIZED_FIELD])
    document_matrix = cvec.transform(tokenized_df[TOKENIZED_FIELD])
    batches = np.linspace(0, tokenized_df.shape[0], _NUM_VOCAB_BATCHES).astype(
        int)
    i = 0
    tf = []
    while i < len(batches) - 1:
        if i % 100 == 0:
            logs.info("%s of %s vocab batches" % (str(i), str(len(batches))))
        batch_result = np.sum(
            document_matrix[batches[i]: batches[i + 1]].toarray(), axis=0
        )
        tf.append(batch_result)
        i += 1
    word_count_df = pd.DataFrame(
        [np.sum(tf, axis=0)], columns=cvec.get_feature_names()
    ).transpose()
    # Now organize everything into the dataframes
    word_count_df.columns = [CNT]
    word_count_df.index.name = WORD
    return word_count_df


def calc_p_word(word_count_df):
    # p(word)
    word_count_df[PROP] = word_count_df[CNT] / float(sum(word_count_df[CNT]))
    vocab_counts_df = pd.DataFrame(
        word_count_df.sort_values(by=CNT, ascending=False))
    vocab_counts_df[VOCAB] = vocab_counts_df.index
    return vocab_counts_df


def filter_vocab(vocab_counts_df):
    # TODO: Add warnings (which words are missing) to log file?
    filtered_vocab_counts_df = vocab_counts_df.drop(_CLOSED_CLASS,
                                                    errors="ignore")
    filtered_count = filtered_vocab_counts_df[CNT]
    filtered_count_denom = float(sum(filtered_vocab_counts_df[CNT]))
    filtered_vocab_counts_df[PROP] = filtered_count / filtered_count_denom
    return filtered_vocab_counts_df


## Figures ##

def make_fig_lengths(tokenized_df, length_field):
    fig_tok_length, axs = plt.subplots(figsize=(15, 6), dpi=150)
    sns.histplot(data=tokenized_df[length_field], kde=True, bins=100, ax=axs)
    sns.rugplot(data=tokenized_df[length_field], ax=axs)
    return fig_tok_length


def make_npmi_fig(paired_results, subgroup_pair):
    subgroup1, subgroup2 = subgroup_pair
    UI_results = pd.DataFrame()
    if "npmi-bias" in paired_results:
        UI_results["npmi-bias"] = paired_results["npmi-bias"].astype(float)
    UI_results[subgroup1 + "-npmi"] = paired_results["npmi"][
        subgroup1 + "-npmi"
        ].astype(float)
    UI_results[subgroup1 + "-count"] = paired_results["count"][
        subgroup1 + "-count"
        ].astype(int)
    if subgroup1 != subgroup2:
        UI_results[subgroup2 + "-npmi"] = paired_results["npmi"][
            subgroup2 + "-npmi"
            ].astype(float)
        UI_results[subgroup2 + "-count"] = paired_results["count"][
            subgroup2 + "-count"
            ].astype(int)
    return UI_results.sort_values(by="npmi-bias", ascending=True)


## Input/Output ###


def define_subgroup_files(subgroup_list, pmi_dataset_cache_dir):
    """
    Sets the file ids for the input identity terms
    :param subgroup_list: List of identity terms
    :return:
    """
    subgroup_files = {}
    for subgroup in subgroup_list:
        # TODO: Should the pmi, npmi, and count just be one file?
        subgroup_npmi_fid = pjoin(pmi_dataset_cache_dir, subgroup + "_npmi.csv")
        subgroup_pmi_fid = pjoin(pmi_dataset_cache_dir, subgroup + "_pmi.csv")
        subgroup_cooc_fid = pjoin(pmi_dataset_cache_dir, subgroup + "_vocab_cooc.csv")
        subgroup_files[subgroup] = (
            subgroup_npmi_fid,
            subgroup_pmi_fid,
            subgroup_cooc_fid,
        )
    return subgroup_files


## Input/Output ##

def write_subgroup_npmi_data(subgroup, subgroup_dict, subgroup_files):
    """
    Saves the calculated nPMI statistics to their output files.
    Includes the npmi scores for each identity term, the pmi scores, and the
    co-occurrence counts of the identity term with all the other words
    :param subgroup: Identity term
    :return:
    """
    subgroup_fids = subgroup_files[subgroup]
    subgroup_npmi_fid, subgroup_pmi_fid, subgroup_cooc_fid = subgroup_fids
    subgroup_dfs = subgroup_dict[subgroup]
    subgroup_cooc_df, subgroup_pmi_df, subgroup_npmi_df = subgroup_dfs
    with open(subgroup_npmi_fid, "w+") as f:
        subgroup_npmi_df.to_csv(f)
    with open(subgroup_pmi_fid, "w+") as f:
        subgroup_pmi_df.to_csv(f)
    with open(subgroup_cooc_fid, "w+") as f:
        subgroup_cooc_df.to_csv(f)
