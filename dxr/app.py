from cStringIO import StringIO
from functools import partial
from itertools import chain
from logging import StreamHandler
from os.path import join, basename
from sys import stderr
from time import time
from mimetypes import guess_type
from urllib import quote_plus

from flask import (Blueprint, Flask, send_from_directory, current_app,
                   send_file, request, redirect, jsonify, render_template,
                   url_for)
from funcy import merge
from pyelasticsearch import ElasticSearch
from werkzeug.exceptions import NotFound

from dxr.build import linked_pathname
from dxr.exceptions import BadTerm
from dxr.filters import FILE, LINE
from dxr.lines import html_line
from dxr.mime import icon, is_image
from dxr.plugins import plugins_named
from dxr.query import Query, filter_menu_items
from dxr.utils import non_negative_int, TEMPLATE_DIR, decode_es_datetime


# Look in the 'dxr' package for static files, etc.:
dxr_blueprint = Blueprint('dxr_blueprint',
                          'dxr',
                          template_folder=TEMPLATE_DIR,
                          # static_folder seems to register a "static" route
                          # with the blueprint so the url_prefix (set later)
                          # takes effect for static files when found through
                          # url_for('static', ...).
                          static_folder='static')


def make_app(instance_path):
    """Return a DXR application which looks in the given folder for
    configuration.

    Also set up the static and template folder.

    """
    app = Flask('dxr', instance_path=instance_path)

    # Load the special config file generated by dxr-build:
    app.config.from_pyfile(join(app.instance_path, 'config.py'))

    app.register_blueprint(dxr_blueprint, url_prefix=app.config['WWW_ROOT'])

    # Log to Apache's error log in production:
    app.logger.addHandler(StreamHandler(stderr))

    # Make an ES connection pool shared among all threads:
    app.es = ElasticSearch(app.config['ES_HOSTS'])

    return app


@dxr_blueprint.route('/')
def index():
    config = current_app.config
    return redirect(url_for('.browse', tree=config['DEFAULT_TREE']))


@dxr_blueprint.route('/<tree>/search')
def search(tree):
    """Normalize params, and dispatch between JSON- and HTML-returning
    searches, based on Accept header.

    """
    # Normalize querystring params:
    config = current_app.config
    if tree not in config['TREES']:
        raise NotFound('No such tree as %s' % tree)
    req = request.values
    query_text = req.get('q', '')
    offset = non_negative_int(req.get('offset'), 0)
    limit = min(non_negative_int(req.get('limit'), 100), 1000)
    is_case_sensitive = req.get('case') == 'true'

    # Make a Query:
    query = Query(partial(current_app.es.search,
                          index=config['ES_ALIASES'][tree]),
                  query_text,
                  plugins_named(config['TREES'][tree]['enabled_plugins']),
                  is_case_sensitive=is_case_sensitive)

    # Fire off one of the two search routines:
    searcher = _search_json if _request_wants_json() else _search_html
    return searcher(query, tree, query_text, is_case_sensitive, offset, limit, config)


def _search_json(query, tree, query_text, is_case_sensitive, offset, limit, config):
    """Do a normal search, and return the results as JSON."""
    try:
        # Convert to dicts for ease of manipulation in JS:
        results = [{'icon': icon,
                    'path': path,
                    'lines': [{'line_number': nb, 'line': l} for nb, l in lines]}
                   for icon, path, lines in query.results(offset, limit)]
    except BadTerm as exc:
        return jsonify({'error_html': exc.reason, 'error_level': 'warning'}), 400

    return jsonify({
        'www_root': config['WWW_ROOT'],
        'tree': tree,
        'results': results,
        'tree_tuples': _tree_tuples(config['TREES'], tree, query_text, is_case_sensitive)})


def _search_html(query, tree, query_text, is_case_sensitive, offset, limit, config):
    """Search a few different ways, and return the results as HTML.

    Try a "direct search" (for exact identifier matches, etc.). If that
    doesn't work, fall back to a normal search.

    """
    should_redirect = request.values.get('redirect') == 'true'

    # Try for a direct result:
    if should_redirect:  # always true in practice?
        result = query.direct_result()
        if result:
            path, line = result
            # TODO: Does this escape query_text properly?
            return redirect(
                '%s/%s/source/%s?from=%s%s#%i' %
                (config['WWW_ROOT'],
                 tree,
                 path,
                 query_text,
                 '&case=true' if is_case_sensitive else '',
                 line))

    # Try a normal search:
    template_vars = {
            'filters': filter_menu_items(
                plugins_named(config['TREES'][tree]['enabled_plugins'])),
            'generated_date': config['GENERATED_DATE'],
            'google_analytics_key': config['GOOGLE_ANALYTICS_KEY'],
            'is_case_sensitive': is_case_sensitive,
            'query': query_text,
            'search_url': url_for('.search',
                                  tree=tree,
                                  q=query_text,
                                  redirect='false'),
            'tree': tree,
            'tree_tuples': _tree_tuples(config['TREES'], tree, query_text, is_case_sensitive),
            'www_root': config['WWW_ROOT']}

    try:
        results = list(query.results(offset, limit))
    except BadTerm as exc:
        return render_template('error.html',
                               error_html=exc.reason,
                               **template_vars), 400

    return render_template('search.html', results=results, **template_vars)


def _tree_tuples(trees, tree, query_text, is_case_sensitive):
    return [(t,
             url_for('.search',
                     tree=t,
                     q=query_text,
                     **({'case': 'true'} if is_case_sensitive else {})),
             values['description'])
            for t, values in trees.iteritems()]


@dxr_blueprint.route('/<tree>/raw/<path:path>')
def raw(tree, path):
    """Send raw data at path from tree, for binary things like images."""
    query = {
        'filter': {
            'term': {
                'path': path
            }
        }
    }
    index = current_app.config['ES_ALIASES'][tree]
    results = current_app.es.search(
            query,
            index=index,
            doc_type=FILE,
            size=1)
    try:
        # we explicitly get index 0 because there should be exactly 1 result
        data = results['hits']['hits'][0]['_source']['raw_data'][0]
    except IndexError: # couldn't find the image
        raise NotFound
    data_file = StringIO(data.decode('base64'))
    return send_file(data_file, mimetype=guess_type(path)[0])


def _es_alias_or_not_found(tree):
    """Return the elasticsearch alias for a tree, or raise NotFound."""
    try:
        return current_app.config['ES_ALIASES'][tree]
    except KeyError:
        raise NotFound


@dxr_blueprint.route('/<tree>/source/')
@dxr_blueprint.route('/<tree>/source/<path:path>')
def browse(tree, path=''):
    """Show a directory listing or a single file from one of the trees."""
    # Fetch ES FILE doc.
    # Else:
    #   Query for the FILE where path == path.
    #   Query for all the LINEs.
    #   Render it up.


    config = current_app.config

    try:
        return _browse_folder(tree, path, config)
    except NotFound:
        return _browse_file(tree, path, config)


def _filtered_query(index, doc_type, filter, sort=None, size=1, include=None, exclude=None):
    """Do a simple, filtered term query, returning an iterable of _sources.

    This is just a mindless upfactoring. It probably shouldn't be blown up
    into a full-fledged API.

    ``include`` and ``exclude`` are mutually exclusive for now.

    """
    query = {
            'query': {
                'filtered': {
                    'query': {
                        'match_all': {}
                    },
                    'filter': {
                        'term': filter
                    }
                }
            }
        }
    if sort:
        query['sort'] = sort
    if include is not None:
        query['_source'] = {'include': include}
    elif exclude is not None:
        query['_source'] = {'exclude': exclude}
    return [x['_source'] for x in current_app.es.search(
        query,
        index=index,
        doc_type=doc_type,
        size=size)['hits']['hits']]


def _browse_folder(tree, path, config):
    """Return a rendered folder listing for folder ``path``.

    Search for FILEs having folder == path. If any matches, render the folder
    listing. Otherwise, raise NotFound.

    """
    files_and_folders = _filtered_query(
        _es_alias_or_not_found(tree),
        FILE,
        filter={'folder': path},
        sort=[{'is_folder': 'desc'}, 'name'],
        size=10000,
        exclude=['raw_data'])
    if not files_and_folders:
        raise NotFound

    return render_template(
        'folder.html',
        # Common template variables:
        www_root=config['WWW_ROOT'],
        tree=tree,
        tree_tuples=[
            (t_name,
             url_for('.parallel', tree=t_name, path=path),
             t_value['description'])
            for t_name, t_value in config['TREES'].iteritems()],
        generated_date=config['GENERATED_DATE'],
        google_analytics_key=config['GOOGLE_ANALYTICS_KEY'],
        paths_and_names=linked_pathname(path, tree),
        filters=filter_menu_items(
            plugins_named(config['TREES'][tree]['enabled_plugins'])),
        # Autofocus only at the root of each tree:
        should_autofocus_query=path == '',

        # Folder template variables:
        name=basename(path) or tree,
        path=path,
        files_and_folders=[
            (_icon_class_name(f),
             f['name'],
             decode_es_datetime(f['modified']) if 'modified' in f else None,
             f.get('size'),
             url_for('.browse', tree=tree, path=f['path'][0]))
            for f in files_and_folders])


def _browse_file(tree, path, config):
    """Return a rendered page displaying a source file.

    If there is no such file, raise NotFound.

    """
    def sidebar_links(sections):
        """Return data structure to build nav sidebar from. ::

            [('Section Name', [{'icon': ..., 'title': ..., 'href': ...}])]

        """
        # Sort by order, resolving ties by section name:
        return sorted(sections, key=lambda section: (section['order'],
                                                     section['heading']))

    # Grab the FILE doc, just for the sidebar nav links:
    files = _filtered_query(
        _es_alias_or_not_found(tree),
        FILE,
        filter={'path': path},
        size=1,
        include=['links'])
    if not files:
        raise NotFound
    links = files[0].get('links', [])

    lines = _filtered_query(
        _es_alias_or_not_found(tree),
        LINE,
        filter={'path': path},
        size=1000000,
        include=['content', 'tags', 'annotations'])

    # Common template variables:
    common = {
        'www_root': config['WWW_ROOT'],
        'tree': tree,
        'tree_tuples':
            [(tree_name,
              url_for('.parallel', tree=tree_name, path=path),
              tree_values['description'])
            for tree_name, tree_values in config['TREES'].iteritems()],
        'generated_date': config['GENERATED_DATE'],
        'google_analytics_key': config['GOOGLE_ANALYTICS_KEY'],
        'filters': filter_menu_items(
            plugins_named(config['TREES'][tree]['enabled_plugins'])),
    }

    # File template variables
    file_vars = {
        'paths_and_names': linked_pathname(path, tree),
        'icon': icon(path),
        'path': path,
        'name': basename(path),
    }

    if is_image(path):
        return render_template(
            'image_file.html',
            **merge(common, file_vars))
    else:  # For now, we don't index binary files, so this is always a text one
        return render_template(
            'text_file.html',
            **merge(common, file_vars, {
                # Someday, it would be great to stream this and not concretize
                # the whole thing in RAM. The template will have to quit
                # looping through the whole thing 3 times.
                'lines': [(html_line(doc['content'][0], doc.get('tags', [])),
                           doc.get('annotations', [])) for doc in lines],
                'is_text': True,
                'sections': sidebar_links(links)}))


@dxr_blueprint.route('/<tree>/')
@dxr_blueprint.route('/<tree>')
def tree_root(tree):
    """Redirect requests for the tree root instead of giving 404s."""
    # Don't do a redirect and then 404; that's tacky:
    _es_alias_or_not_found(tree)
    return redirect(tree + '/source/')


@dxr_blueprint.route('/<tree>/parallel/')
@dxr_blueprint.route('/<tree>/parallel/<path:path>')
def parallel(tree, path=''):
    """If a file or dir parallel to the given path exists in the given tree,
    redirect to it. Otherwise, redirect to the root of the given tree.

    Deferring this test lets us avoid doing 50 queries when drawing the Switch
    Tree menu when 50 trees are indexed: we check only when somebody actually
    chooses something.

    """
    config = current_app.config
    www_root = config['WWW_ROOT']

    files = _filtered_query(
        _es_alias_or_not_found(tree),
        FILE,
        filter={'path': path.rstrip('/')},
        size=1,
        include=[])  # We don't really need anything.
    return redirect(('{root}/{tree}/source/{path}' if files else
                     '{root}/{tree}/source/').format(root=www_root,
                                                     tree=tree,
                                                     path=path))


def _icon_class_name(file_doc):
    """Return a string for the CSS class of the icon for file document."""
    if file_doc['is_folder']:
        return 'folder'
    class_name = icon(file_doc['name'])
    # for small images, we can turn the image into icon via javascript
    # if bigger than the cutoff, we mark it as too big and don't do this
    if file_doc['size'] > current_app.config['MAX_THUMBNAIL_SIZE']:
        class_name += " too_fat"
    return class_name


def _request_wants_json():
    """Return whether the current request prefers JSON.

    Why check if json has a higher quality than HTML and not just go with the
    best match? Because some browsers accept on */* and we don't want to
    deliver JSON to an ordinary browser.

    """
    # From http://flask.pocoo.org/snippets/45/
    best = request.accept_mimetypes.best_match(['application/json',
                                                'text/html'])
    return (best == 'application/json' and
            request.accept_mimetypes[best] >
                    request.accept_mimetypes['text/html'])
