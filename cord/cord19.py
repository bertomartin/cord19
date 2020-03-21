import gc
import json
from functools import reduce

import ipywidgets as widgets
import nltk
import numpy as np
import pandas as pd
import requests
from IPython.display import display
from requests import HTTPError

from cord.core import parallel, ifnone, add, render_html
from cord.text import preprocess, extract_publish_date, shorten

nltk.download("punkt")
from rank_bm25 import BM25Okapi
from nltk.corpus import stopwords

english_stopwords = list(set(stopwords.words('english')))
import pickle
from pathlib import Path, PurePath


class Author:

    def __init__(self, first=None, last=None, middle=None):
        self.first = ifnone(first, '')
        self.last = ifnone(last, '')

    def __repr__(self):
        return f'{self.first} {self.last}'


class JPaper:

    def __init__(self, paper):
        self.paper = paper
        self.sha = paper['paper_id']
        self.abstract = '\n'.join([a['text'] for a in paper['abstract']])
        self.title = paper['metadata']['title']
        self.authors = [Author(a.get('first'), a.get('last'), a.get('middle'))
                        for a in paper['metadata']['authors']]
        self.sections = [{s['section']: s['text']} for s in paper['body_text']]

    def _repr_html_(self):
        _html = f'<h4>{self.title}</h4>'
        return _html

    def __repr__(self):
        return f'{self.title}'


def load_json(json_file):
    with open(json_file, 'r') as f:
        contents = json.load(f)
        jpaper = JPaper(contents)
    return jpaper


class JCatalog:

    def __init__(self, papers):
        self.papers = papers
        self.paper_index = {p.sha: p for p in self.papers}
        self.index = pd.Series(self.papers, index=[p.sha for p in papers])

    @classmethod
    def load(cls, json_catalog_path):
        print('Load JSON from', json_catalog_path)
        papers = parallel(load_json, list(json_catalog_path.glob('*.json')))
        return cls(papers)

    def __getitem__(self, item):
        if isinstance(item, int):
            return self.papers[item]
        return self.index.loc[item].values[0]

    def __len__(self):
        return len(self.papers)

    def __add__(self, o):
        return JCatalog(self.papers + o.papers)


def get(url, timeout=6):
    try:
        r = requests.get(url, timeout=timeout)
        return r.text
    except ConnectionError:
        print(f'Cannot connect to {url}')
        print(f'Remember to turn Internet ON in the Kaggle notebook settings')
    except HTTPError:
        print('Got http error', r.status, r.text)


_DISPLAY_COLS = ['sha', 'title', 'abstract', 'publish_time', 'authors', 'has_full_text']
_RESEARCH_PAPERS_SAVE_FILE = 'ResearchPapers.pickle'
_COVID = ['sars-cov-2', '2019-ncov', 'covid-19', 'covid-2019', 'wuhan', 'hubei', 'coronavirus']


class ResearchPapers:

    def __init__(self, metadata, json_catalog):
        self.metadata = metadata
        self.json_catalog = json_catalog
        print('Building a BM25 index')
        index_tokens = self._create_index_tokens()
        # Add antiviral column
        self.metadata['antivirals'] = index_tokens.apply(lambda t:
                                                         ','.join([token for token in t if token.endswith('vir')]))
        # Does it have any covid term?
        self.metadata['covid_related'] = index_tokens.apply(lambda t: any([covid_term in t for covid_term in _COVID]))
        self.bm25 = BM25Okapi(index_tokens.tolist())
        self.num_results = 10
        gc.collect()

    def __getitem__(self, item):
        if isinstance(item, int):
            paper = self.metadata.iloc[item]
        else:
            paper = self.metadata[self.metadata.sha == item]
        # Look up for the corresponding json paper if it exists
        if isinstance(paper.sha, float) and np.isnan(paper.sha):
            json_paper = None  # No sha on the metadata row
        else:
            json_paper = self.json_catalog[paper.sha]
        return Paper(paper, json_paper)

    def covid_related(self):
        _metadata = self.metadata[self.metadata.covid_related].copy()
        return ResearchPapers(_metadata, self.json_catalog)

    def __len__(self):
        return len(self.metadata)

    def head(self, n):
        return ResearchPapers(self.metadata.head(n).copy().reset_index(drop=True), self.json_catalog)

    def tail(self, n):
        return ResearchPapers(self.metadata.tail(n).copy().reset_index(drop=True), self.json_catalog)

    def abstracts(self):
        return pd.Series([self.__getitem__(i).abstract() for i in range(len(self))])

    def titles(self):
        return pd.Series([self.__getitem__(i).title() for i in range(len(self))])

    def _repr_html_(self):
        return self.metadata._repr_html_()

    @staticmethod
    def load_metadata(data_dir='data'):
        data_path = Path(data_dir) / 'CORD-19-research-challenge/2020-03-13'
        metadata_path = PurePath(data_path) / 'all_sources_metadata_2020-03-13.csv'
        metadata = pd.read_csv(metadata_path,
                               dtype={'Microsoft Academic Paper ID': str,
                                      'pubmed_id': str})

        # Convert the doi to a url
        def doi_url(d): return f'http://{d}' if d.startswith('doi.org') else f'http://doi.org/{d}'

        metadata.doi = metadata.doi.fillna('').apply(doi_url)

        # Set the abstract to the paper title if it is null
        metadata.abstract = metadata.abstract.fillna(metadata.title).fillna('')
        # Some papers are duplicated since they were collected from separate sources. Thanks Joerg Rings
        duplicate_paper = ~(metadata.title.isnull() | metadata.abstract.isnull()) \
                          & (metadata.duplicated(subset=['title', 'abstract']))
        metadata = metadata[~duplicate_paper].reset_index(drop=True)

        metadata['published'] = extract_publish_date(metadata.publish_time)
        return metadata.drop(columns=['publish_time'])

    @classmethod
    def from_data_dir(cls, data_dir='data'):
        data_path = Path(data_dir) / 'CORD-19-research-challenge/2020-03-13'
        metadata = cls.load_metadata(data_dir)

        # the Json files
        biorxiv = data_path / 'biorxiv_medrxiv/biorxiv_medrxiv'
        comm_use = data_path / 'comm_use_subset/comm_use_subset'
        noncomm_use = data_path / 'noncomm_use_subset/noncomm_use_subset'
        pmc_custom_license = data_path / 'pmc_custom_license/pmc_custom_license'

        json_paths = [biorxiv, comm_use, noncomm_use, pmc_custom_license]
        catalogs = [JCatalog.load(p) for p in json_paths]
        # Combine the catalogs into one
        json_catalog = reduce(add, catalogs)

        return cls(metadata, json_catalog)

    @staticmethod
    def from_pickle(save_dir='data'):
        save_path = PurePath(save_dir) / _RESEARCH_PAPERS_SAVE_FILE
        with open(save_path, 'rb') as f:
            return pickle.load(f)

    def save(self, save_dir='data'):
        save_path = PurePath(save_dir) / _RESEARCH_PAPERS_SAVE_FILE
        print('Saving to', save_path)
        with open(save_path, 'wb') as f:
            pickle.dump(self, f)

    def _create_index_tokens(self):
        abstracts = self.metadata[['sha', 'abstract']]
        json_abstracts = self.json_catalog \
            .index.apply(lambda p: p.abstract) \
            .fillna('').to_frame(name='json_abstract') \
            .reset_index().rename(columns={'index': 'sha'})
        abs_merged = abstracts.merge(json_abstracts, on='sha', how='left')
        abstract_col = abs_merged.abstract + ' ' + abs_merged.json_abstract.fillna('')
        abstract_col = abstract_col.fillna('')
        abstract_tokens = abstract_col.apply(preprocess)
        return abstract_tokens

    def search(self, search_string, n_results=None):
        n_results = n_results or self.num_results
        search_terms = preprocess(search_string)
        doc_scores = self.bm25.get_scores(search_terms)
        ind = np.argsort(doc_scores)[::-1][:n_results]
        results = self.metadata.iloc[ind].copy()
        results['Score'] = doc_scores[ind].round(1)
        results = results[results.Score > 0].reset_index(drop=True)
        return SearchResults(results)

    def _search_papers(self, SearchTerms: str):
        search_results = self.search(SearchTerms)
        if len(search_results) > 0:
            display(search_results)
        return search_results

    def searchbar(self, search_terms='cruise ship', num_results=10):
        self.num_results = num_results
        return widgets.interactive(self._search_papers, SearchTerms=search_terms)


class Paper:
    '''
    A single research paper
    '''

    def __init__(self, item, json_paper):
        self.sha = item.sha
        self.paper = item.fillna('').T
        self.paper.columns = ['Value']
        self.json_paper = json_paper

    def doi(self):
        return self.paper.loc['doi'].values[0]

    def html(self):
        '''
        Load the paper from doi.org and display as HTML. Requires internet to be ON
        '''
        text = get(self.doi())
        return widgets.HTML(text)

    def text(self):
        '''
        Load the paper from doi.org and display as text. Requires Internet to be ON
        '''
        if self.json_paper is not None:
            return self.json_paper.text()
        return get(self.doi())

    def abstract(self):
        _abstract = self.paper.loc['abstract'].values[0]
        if _abstract:
            return _abstract
        # Take the abstract from the json paper
        elif self.json_paper:
            _abstract = self.json_paper.abstract
            if _abstract:
                return _abstract
        return ''

    def title(self):
        if self.json_paper:
            title = self.json_paper.title
            if title:
                return title
        return self.paper.loc['title'].values[0]

    def authors(self, split=False):
        if self.json_paper:
            return self.json_paper.authors
        '''
        Get a list of authors
        '''
        authors = self.paper.loc['authors'].values[0]
        if not authors:
            return []
        if not split:
            return authors
        if authors.startswith('['):
            authors = authors.lstrip('[').rstrip(']')
            return [a.strip().replace("\'", "") for a in authors.split("\',")]

        # Todo: Handle cases where author names are separated by ","
        return [a.strip() for a in authors.split(';')]

    def _repr_html_(self):
        return render_html('Paper', paper=self)


class SearchResults:

    def __init__(self, data: pd.DataFrame):
        self.results = data.dropna(subset=['title'])
        self.results.authors = self.results.authors.apply(str).replace("'", '').replace('[', '').replace(']', '')
        self.columns = [col for col in ['sha', 'title', 'authors', 'published', 'Score'] if col in data]

    def __getitem__(self, item):
        return Paper(self.results.loc[item])

    def __len__(self):
        return len(self.results)

    def _results_view(self, search_results):
        return [{'title': rec['title'],
                 'authors': rec['authors'],
                 'abstract': shorten(rec['abstract']),
                 'published': rec['published']
                 }
                for rec in search_results.to_dict('records')]

    def _repr_html_(self):
        # search_results=self._results_view(self.results))
        display_cols = [col for col in self.columns if not col == 'sha']
        return render_html('SearchResults', search_results=self.results[display_cols])