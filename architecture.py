"""
architecture.py - Application Support estate architecture graph.

Builds a live graph of the whole estate (every active tower, every active
application, every server and every inter-application interface) for the
layered "semantic zoom" Architecture View, which drills:

    L1 estate  ->  L2 tower  ->  L3 application

The hard part is not the drawing (that happens client-side); it is deciding
what is actually connected to what. That is the resolution rule below:
an External Interface is matched to the application on the other end by
HOSTNAME first - a host listed under one application's Infrastructure
appearing as another application's interface host - and only then by
application name, which is the older and looser rule kept as a fallback.
Anything that matches nothing is a genuine third-party system; it is
labelled with the Application name the Utility module's server inventory
records against its hostname, falling back to its own interface name when
the host is not in that inventory either.

The same graph is read a second way by the Enterprise Architect view, which
groups the identical set of applications by APPLICATION CATEGORY (the business
area) instead of by delivery tower:

    L1 enterprise (domains)  ->  L2 domain  ->  L3 application

Both readings share this one payload on purpose - two builders would drift,
and an estate that disagrees with itself about what is connected to what is
worse than no diagram at all. The category grouping is therefore emitted
alongside the tower grouping rather than from a second query.

Everything is recomputed from the database on every request, so adding a
tower, an application, a category, a server or an interface immediately
changes both views.
"""

from datetime import datetime, timezone

from sqlalchemy import text

from .insights import PROTOCOL_OTHER, classify_protocol
from .models import (
    AppSupportTower, ApplicationCategory, ApplicationDetails,
    ApplicationTechStack, ExternalInterfaces, FlowDirection, ServerDetails,
    SupportTeam, VendorDetails, BusinessUsers,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _norm(name):
    """Normalise a name for matching: case/space/punctuation-insensitive."""
    return ''.join(ch for ch in str(name or '').lower() if ch.isalnum())


def _enum_value(value, fallback=''):
    """Enum member or raw string -> display string (SQLite stores strings)."""
    if value is None:
        return fallback
    return getattr(value, 'value', str(value))


# ---------------------------------------------------------------------------
# Host-based interface resolution
#
# An interface is matched to the application on the other end by HOSTNAME
# first, and only then by application name (the older, looser rule kept as a
# fallback). Hostnames are unambiguous; names drift.
#
# The join key is the hostname: when a host listed under one application's
# Infrastructure (ServerDetails.server_name) also appears as the host of
# another application's External Interface, the two applications are
# connected, and the resolved endpoint is DISPLAYED as the owning
# application's name rather than the raw interface name.
# ---------------------------------------------------------------------------

#: The ExternalInterfaces column that carries the peer hostname, and the
#: column to fall back on when it is blank. Named constants because which
#: field actually holds the host is a data convention, not a law - estates
#: that record the host in `interface_name` only need these two flipped.
HOST_FIELD = 'external_server'
HOST_FALLBACK_FIELD = 'interface_name'

#: Resolution provenance, carried through to the JSON graph so the UI can
#: tell a hostname-backed edge from a name-guessed one.
MATCH_HOST = 'host'
MATCH_NAME = 'name'

#: What a connection is called when the record does not say how it is made.
INTERFACE_FALLBACK = 'Interface'

#: The channel of a record that does not say how the two systems are joined.
#: Deliberately not a channel of its own - see fold_unrecorded_channels().
CHANNEL_UNRECORDED = None


def channel_key(connectivity):
    """The CHANNEL a free-text connectivity label names.

    An edge cannot be identified by the label itself. `connectivity_type` is
    free text, typed independently at each end of the same wire, so the two
    owners of one integration routinely word it differently - "SFTP" against
    "File Transfer", "REST API" against "HTTPS", "MQ" against "Kafka
    topic" - and identifying an edge by the wording made those two
    connections, which is what listed the peer twice on the L3 sheet.

    A label is therefore reduced to the integration STYLE it describes, read
    with the same taxonomy the insights and brief layers read it with, so the
    whole module agrees about what a channel is. Wording that the taxonomy
    cannot place stands on its own normalised text instead: two labels nobody
    can classify are not evidence of one channel.

    Returns None when nothing was recorded at all.
    """
    label = (connectivity or '').strip()
    if not label:
        return CHANNEL_UNRECORDED
    family = classify_protocol(label)
    if family != PROTOCOL_OTHER:
        return family
    return 'other:' + _norm(label)


def build_host_registry(server_rows):
    """Index every server in the estate by normalised hostname.

    `server_rows` is an iterable of (app_id, app_name, server_id, server_name)
    - deliberately plain tuples rather than ORM rows so the registry can be
    built and tested without a database.

    Two applications claiming the same hostname is a data-quality problem, not
    something to resolve silently: the first owner wins and the collision is
    reported, so the estate can be corrected at source.

    Returns (registry, warnings):
      registry : normalised host -> {app_id, app_name, server_id, host}
      warnings : [{'type': 'duplicate_host', 'host': ..., 'app_ids': [...]}]
    """
    registry = {}
    collisions = {}
    for app_id, app_name, server_id, server_name in server_rows:
        key = _norm(server_name)
        if not key:
            continue
        owner = registry.get(key)
        if owner is None:
            registry[key] = {
                'app_id': app_id,
                'app_name': app_name or '',
                'server_id': server_id,
                'host': (server_name or '').strip(),
            }
            continue
        if owner['app_id'] == app_id:
            continue        # same app listing the host twice - harmless
        entry = collisions.setdefault(
            key, {'type': 'duplicate_host', 'host': owner['host'],
                  'app_ids': [owner['app_id']]})
        if app_id not in entry['app_ids']:
            entry['app_ids'].append(app_id)

    warnings = [collisions[k] for k in sorted(collisions)]
    return registry, warnings


def build_name_index(app_rows):
    """Index every application by normalised name, for the fallback rule.

    `app_rows` is an iterable of (app_id, app_name). First name wins, matching
    the host registry's rule; duplicate application names are a naming problem
    the estate view should not try to guess its way through.

    Returns: normalised name -> {app_id, app_name}
    """
    index = {}
    for app_id, app_name in app_rows:
        key = _norm(app_name)
        if key and key not in index:
            index[key] = {'app_id': app_id, 'app_name': app_name or ''}
    return index


def build_utility_name_index(rows):
    """Index the Utility module's server inventory by normalised hostname.

    `rows` is an iterable of (server_name, application_name) - the columns
    the Utility module's Servers page imports from Excel. Rows with a blank
    hostname or a blank application name teach us nothing and are skipped;
    for duplicates the first name wins, matching the host registry's rule.

    Returns: normalised hostname -> application name.
    """
    index = {}
    for server_name, application_name in rows:
        key = _norm(server_name)
        name = (application_name or '').strip()
        if key and name and key not in index:
            index[key] = name
    return index


def _load_utility_names(db_session):
    """The Utility server inventory as a hostname -> application-name map.

    Read with plain SQL rather than by importing the Utility module: the
    architecture view only depends on the `server_data` table's contract
    (server_name, application_name), not on the module being mounted. On
    any failure - table never created, module not deployed - the estate
    view must still render, so this degrades to an empty map. The probe
    runs inside a SAVEPOINT so that failing leaves the rest of the session
    untouched instead of rolling all of it back.
    """
    try:
        nested = db_session.begin_nested()
    except Exception:
        return {}
    try:
        rows = db_session.execute(text(
            'SELECT server_name, application_name FROM server_data '
            'WHERE server_name IS NOT NULL'
        )).fetchall()
        nested.commit()
    except Exception:
        nested.rollback()
        return {}
    return build_utility_name_index(rows)


def _load_category_rows(db_session):
    """Every application category as (id, name, description, sort, active).

    Degrades to an empty list the same way _load_utility_names() does, and
    for the same reason: categories arrived after the architecture view did,
    so an estate whose schema has not caught up yet must still get its
    diagram - it simply shows every application as Uncategorised. The probe
    runs inside a SAVEPOINT so a failure cannot roll back the rest of the
    session.
    """
    try:
        nested = db_session.begin_nested()
    except Exception:
        return []
    try:
        rows = [
            (c.id, c.name, c.description, c.sort_order, c.is_active)
            for c in db_session.query(ApplicationCategory)
            .order_by(ApplicationCategory.sort_order,
                      ApplicationCategory.name).all()
        ]
        nested.commit()
    except Exception:
        nested.rollback()
        return []
    return rows


#: Technology categories, in the order an architect reads a stack: what it
#: is written in, what it stores data in, what it runs on, what it uses.
#: Kept here rather than imported from models.TECH_CATEGORIES because the
#: ORDER is a display decision of this view, not a schema fact.
TECH_ORDER = ('language', 'database', 'os', 'tool')


def _load_tech_rows(db_session, app_ids):
    """Every application's recorded technology, as {app_id: [entry, ...]}.

    The Technical Details tab arrived after the architecture view did, so an
    estate whose schema has not caught up must still get its diagram: this
    degrades to an empty map exactly the way _load_utility_names() and
    _load_category_rows() do, and for the same reason. The probe runs inside
    a SAVEPOINT so a failure cannot roll back the rest of the session.

    Entries are ordered by TECH_ORDER then by name, so two applications with
    the same stack always read it in the same sequence.

    Returns: {app_id: [{'category', 'name', 'version'}, ...]}
    """
    if not app_ids:
        return {}
    try:
        nested = db_session.begin_nested()
    except Exception:
        return {}
    try:
        rows = (
            db_session.query(ApplicationTechStack)
            .filter(ApplicationTechStack.application_id.in_(app_ids))
            .all()
        )
        nested.commit()
    except Exception:
        nested.rollback()
        return {}

    grouped = {}
    for row in rows:
        name = (row.item_name or '').strip()
        if not name:
            continue        # a stack row with no technology on it says nothing
        grouped.setdefault(row.application_id, []).append({
            'category': (row.category or '').strip(),
            'name': name,
            'version': (row.version or '').strip(),
        })

    def _sort_key(entry):
        try:
            rank = TECH_ORDER.index(entry['category'])
        except ValueError:
            rank = len(TECH_ORDER)
        return (rank, entry['name'].lower())

    for app_id in grouped:
        grouped[app_id].sort(key=_sort_key)
    return grouped


def resolve_interface(interface_name, external_server, owner_app_id,
                      host_registry, name_index, utility_names=None):
    """Resolve one External Interface to the far end of its connection.

    Tries the host registry first, then the application-name index, and gives
    up gracefully - an interface that matches nothing is a real third-party
    system, not an error. A third-party system is still NAMED as well as the
    data allows: `utility_names` (the Utility server inventory, hostname ->
    application name) supplies its display name when the host is recorded
    there, and only then does the interface's own name stand in.

    An interface pointing at a host inside its OWN application is not a
    connection (an app talking to its own server is just an app), so it never
    produces a self-loop; it falls through to the name rule and, failing that,
    is reported as unresolved.

    Returns {'app_id', 'match', 'via_host', 'display'} where `app_id` is None
    when nothing matched, `match` is 'host' / 'name' / None, `via_host` is the
    hostname that produced a host match, and `display` is the label to show:
    the resolved application's name, else the interface's own name.
    """
    raw_host = (external_server or '').strip() or (interface_name or '').strip()
    host_key = _norm(external_server) or _norm(interface_name)

    owner = host_registry.get(host_key) if host_key else None
    if owner is not None and owner['app_id'] != owner_app_id:
        return {'app_id': owner['app_id'], 'match': MATCH_HOST,
                'via_host': raw_host, 'display': owner['app_name']}

    # Name fallback - the original rule. Interface name first, then the
    # external-server column, since either may carry the peer's app name.
    for candidate in (interface_name, external_server):
        target = name_index.get(_norm(candidate))
        if target is not None and target['app_id'] != owner_app_id:
            return {'app_id': target['app_id'], 'match': MATCH_NAME,
                    'via_host': None, 'display': target['app_name']}

    utility_name = (utility_names or {}).get(host_key)
    return {'app_id': None, 'match': None, 'via_host': None,
            'display': utility_name or (interface_name or external_server
                                        or 'External system').strip()}


# ---------------------------------------------------------------------------
# One connection, one line
#
# A connection between two applications is declared by whoever gets round to
# typing it, and estates routinely declare the same one more than once: both
# owners record their own side of it, or one owner records it twice because
# the peer answers on two hostnames. Those are all the SAME wire, and drawing
# one line per record is what puts a peer in an L3 diagram twice.
#
# The identity of an edge is therefore the pair of applications and the
# CHANNEL - NOT the hostname it resolved through, and not the connectivity
# wording either, both of which are properties of the record rather than of
# the connection. Two owners describing one wire as "SFTP" and as "File
# Transfer" are describing one connection; see channel_key(). A genuinely
# different integration style IS a second connection and keeps its own line.
# Everything that is per-record (the hosts, the wordings, the interface row
# ids, which application declared it) is merged onto the single edge, so
# nothing is lost by collapsing and a reader can still open every row behind
# a line.
# ---------------------------------------------------------------------------

def _merge_declaration(rec, owner_app_id, interface_id, resolved,
                       connectivity=None):
    """Fold one interface row into the edge it describes.

    Hostname provenance is additive: an edge matched by hostname anywhere
    stays hostname-backed even if another record for it only matched by
    name, because the connection IS evidenced - one of the records simply
    says less than the other.

    The record's own CONNECTIVITY WORDING travels with it, in declaration
    order. An edge is identified by its channel rather than by that wording,
    so the two ends of one wire are free to word it differently; keeping both
    is what lets the view still say which words the estate actually used.
    """
    host = (resolved.get('via_host') or '').strip()
    if host and host not in rec['hosts']:
        rec['hosts'].append(host)
    if interface_id is not None and interface_id not in rec['interface_ids']:
        rec['interface_ids'].append(interface_id)
    if owner_app_id not in rec['declared_by']:
        rec['declared_by'].append(owner_app_id)
    label = (connectivity or '').strip()
    if label and label not in rec['connectivities']:
        rec['connectivities'].append(label)
    if resolved.get('match') == MATCH_HOST and rec.get('match') != MATCH_HOST:
        rec['match'] = MATCH_HOST
    if not rec.get('via_host') and host:
        rec['via_host'] = host


def _absorb(rec, other):
    """Fold every declaration carried by `other` onto `rec`, in place.

    `rec` keeps its own src/dst - which is what gives the line its arrow -
    and its own channel; everything that is per-record joins it.
    """
    for host in other['hosts']:
        if host not in rec['hosts']:
            rec['hosts'].append(host)
    for iid in other['interface_ids']:
        if iid not in rec['interface_ids']:
            rec['interface_ids'].append(iid)
    for app_id in other['declared_by']:
        if app_id not in rec['declared_by']:
            rec['declared_by'].append(app_id)
    for label in other['connectivities']:
        if label not in rec['connectivities']:
            rec['connectivities'].append(label)
    if other.get('match') == MATCH_HOST:
        rec['match'] = MATCH_HOST
    if not rec.get('via_host'):
        rec['via_host'] = other.get('via_host')
    return rec


def _fold_opposite(rec, opposite):
    """Merge the mirror-image edge into this one.

    Called once per pair, from the side that sorts first, so the surviving
    edge keeps ITS src/dst (which is what gives the line its arrow) while
    picking up the hosts, wordings and interface rows the other side
    contributed.
    """
    merged = dict(rec)
    merged['hosts'] = list(rec['hosts'])
    merged['interface_ids'] = list(rec['interface_ids'])
    merged['declared_by'] = list(rec['declared_by'])
    merged['connectivities'] = list(rec['connectivities'])
    return _absorb(merged, opposite)


def fold_unrecorded_channels(directed, bidir_keys=None):
    """Attach records that do not say HOW to the channel the pair does name.

    A blank connectivity type is not a second way of joining two systems; it
    is the same connection with a field left empty. So when a pair names
    exactly ONE channel, a record that names none belongs to it - which is
    what stops one end's empty field putting the peer on the L3 sheet twice.
    When the pair names several there is nothing to say which of them the
    blank record meant, so it stays a connection of its own rather than being
    attributed by guesswork, and the gap then shows up as what it is: a
    connection nobody has described.

    `bidir_keys` is updated when the record being folded in ran the other
    way, so a reciprocal declaration still reads as a two-way flow after it
    has been absorbed. Mutates and returns `directed`.
    """
    by_pair = {}
    for key in directed:
        by_pair.setdefault(frozenset((key[0], key[1])), []).append(key)

    for pair in sorted(by_pair, key=sorted):
        keys = by_pair[pair]
        named = sorted(set(k[2] for k in keys
                           if k[2] is not CHANNEL_UNRECORDED))
        if len(named) != 1:
            continue
        channel = named[0]
        for key in [k for k in keys if k[2] is CHANNEL_UNRECORDED]:
            src, dst = key[0], key[1]
            target = (directed.get((src, dst, channel))
                      or directed.get((dst, src, channel)))
            if target is None:
                continue
            if bidir_keys is not None and target['src_app'] != src:
                bidir_keys.add((pair, channel))
            _absorb(target, directed.pop(key))
    return directed


def _record_external(node, app_id, connectivity, channel, direction,
                     interface_id):
    """Fold one unresolved interface row into its third-party system.

    The same collapsing rule as for resolved edges, matched on the same
    CHANNEL rather than on the wording, and for the same reason: an
    application that declares the same feed from the same supplier twice has
    one dependency on that supplier, not two, and an L3 diagram that lists
    the supplier twice is reporting the duplicate record rather than the
    estate.

    A third party has no record of its own to declare the connection back, so
    there is no reciprocal wording to reconcile here and nothing to fold the
    way fold_unrecorded_channels() folds it between two applications: a feed
    nobody has described stays a feed nobody has described.
    """
    # `connectivity` carries the display fallback when the record said
    # nothing about how the feed arrives; the wording list holds only what
    # somebody actually typed, which is exactly when there is a channel.
    wording = connectivity if channel is not CHANNEL_UNRECORDED else ''

    for edge in node['links']:
        if edge['app'] == app_id and edge['channel'] == channel \
                and edge['direction'] == direction:
            if wording and wording not in edge['connectivities']:
                edge['connectivities'].append(wording)
            if interface_id is not None \
                    and interface_id not in edge['interface_ids']:
                edge['interface_ids'].append(interface_id)
                edge['declarations'] = len(edge['interface_ids'])
            return
    node['links'].append({
        'app': app_id, 'connectivity': connectivity, 'channel': channel,
        'connectivities': [wording] if wording else [],
        'direction': direction,
        'interface_ids': [] if interface_id is None else [interface_id],
        'declarations': 1,
    })


def collect_duplicate_declarations(links, externals, app_name_by_id):
    """Connections described by more than one interface record.

    Not an error and not something the diagram should show - the edge is
    already one line - but it IS the thing to correct at source, so it is
    reported the way duplicate hostnames are: named, counted, and pointed at
    the record that needs editing.

    Returns [{'type': 'duplicate_interface', 'label', 'connectivity',
              'declarations', 'interface_ids', 'app_ids'}]
    """
    notes = []
    for link in links:
        if len(link.get('interface_ids') or []) < 2:
            continue
        src = app_name_by_id.get(link['src_app'], 'application %s'
                                 % link['src_app'])
        dst = app_name_by_id.get(link['dst_app'], 'application %s'
                                 % link['dst_app'])
        notes.append({
            'type': 'duplicate_interface',
            'label': '{0} {1} {2}'.format(src, '<->' if link.get('bidir')
                                          else '->', dst),
            'connectivity': link.get('connectivity') or '',
            'declarations': len(link['interface_ids']),
            'interface_ids': list(link['interface_ids']),
            'app_ids': list(link.get('declared_by') or []),
        })
    for ext in externals:
        for edge in ext.get('links') or []:
            if edge.get('declarations', 1) < 2:
                continue
            owner = app_name_by_id.get(edge['app'],
                                       'application %s' % edge['app'])
            notes.append({
                'type': 'duplicate_interface',
                'label': '{0} -> {1}'.format(owner, ext.get('name') or ''),
                'connectivity': edge.get('connectivity') or '',
                'declarations': edge['declarations'],
                'interface_ids': list(edge.get('interface_ids') or []),
                'app_ids': [edge['app']],
            })
    notes.sort(key=lambda n: (-n['declarations'], n['label'].lower()))
    return notes


# ---------------------------------------------------------------------------
# Business-area grouping (the Enterprise Architect view)
#
# The tower grouping answers "who supports this?". The category grouping
# answers "what part of the business does this serve?" - the question an
# architect asks - and it is a pure re-slice of the same applications, so it
# is computed here from the payload that is already built rather than from a
# second pass over the database.
# ---------------------------------------------------------------------------

#: The bucket an application with no category falls into. Zero rather than
#: None because it is used as a key on both sides of the wire, and JSON
#: object keys / JS Map keys handle a number far better than a null.
UNCATEGORISED_ID = 0
UNCATEGORISED_NAME = 'Uncategorised'

#: Sort key given to the Uncategorised bucket so it always lands last: a gap
#: in the data belongs at the end of the landscape, not in the middle of it.
UNCATEGORISED_SORT = 10 ** 6


def build_category_view(app_payload, links, category_rows):
    """Group applications into business-area domains.

    `app_payload` is the list build_estate_graph() has already assembled
    (each entry carrying `id`, `category_id` and `bc`), `links` the resolved
    application-to-application edges, and `category_rows` an iterable of
    (id, name, description, sort_order, is_active) - plain tuples, so this
    can be exercised without a session.

    Which domains appear follows the module's retire-do-not-delete rule: an
    ACTIVE category is always a domain, even with nothing in it, because an
    empty business area is a real finding; a RETIRED one appears only while
    applications still carry it. Anything unclassified collects in the
    Uncategorised bucket, which exists only when it has members.

    Returns (categories, category_links):
      categories      : [{id, name, description, sort_order, active,
                          uncategorised, app_ids, tower_ids, bc_mix}]
      category_links  : [{a, b, count}] per unordered cross-domain pair
    """
    meta = {}
    order = []
    for cat_id, name, description, sort_order, is_active in category_rows:
        meta[cat_id] = {
            'id': cat_id,
            'name': (name or '').strip() or 'Category {0}'.format(cat_id),
            'description': (description or '').strip(),
            'sort_order': sort_order if sort_order is not None else 100,
            'active': bool(is_active),
            'uncategorised': False,
            'app_ids': [],
            'tower_ids': [],
            'bc_mix': {'BC1': 0, 'BC2': 0, 'BC3': 0},
        }
        order.append(cat_id)

    def _bucket(cat_id):
        """The domain an application belongs to, creating the fallback one.

        A category id that no longer resolves - the row was hard-deleted out
        from under the application - is treated as unclassified rather than
        invented, so the landscape never shows a domain nobody can open.
        """
        entry = meta.get(cat_id)
        if entry is not None:
            return entry
        entry = meta.get(UNCATEGORISED_ID)
        if entry is None:
            entry = {
                'id': UNCATEGORISED_ID, 'name': UNCATEGORISED_NAME,
                'description': ('Applications with no business area recorded '
                                'yet.'),
                'sort_order': UNCATEGORISED_SORT, 'active': True,
                'uncategorised': True, 'app_ids': [], 'tower_ids': [],
                'bc_mix': {'BC1': 0, 'BC2': 0, 'BC3': 0},
            }
            meta[UNCATEGORISED_ID] = entry
            order.append(UNCATEGORISED_ID)
        return entry

    domain_of_app = {}
    for entry in app_payload:
        bucket = _bucket(entry.get('category_id'))
        bucket['app_ids'].append(entry['id'])
        if entry.get('bc') in bucket['bc_mix']:
            bucket['bc_mix'][entry['bc']] += 1
        tower_id = entry.get('tower_id')
        if tower_id is not None and tower_id not in bucket['tower_ids']:
            bucket['tower_ids'].append(tower_id)
        domain_of_app[entry['id']] = bucket['id']

    categories = [meta[cat_id] for cat_id in order
                  if meta[cat_id]['active'] or meta[cat_id]['app_ids']]
    categories.sort(key=lambda c: (c['sort_order'], c['name'].lower()))

    pair_counts = {}
    for link in links:
        da = domain_of_app.get(link['src_app'])
        db = domain_of_app.get(link['dst_app'])
        if da is None or db is None or da == db:
            continue    # a link inside one domain belongs to L2, not the ring
        key = (min(da, db), max(da, db))
        pair_counts[key] = pair_counts.get(key, 0) + 1
    category_links = [{'a': a, 'b': b, 'count': n}
                      for (a, b), n in sorted(pair_counts.items())]

    return categories, category_links


# ---------------------------------------------------------------------------
# Layered ("semantic zoom") graph
#
# The whole estate as one JSON payload, with every interface already resolved
# by the rule above. The interactive view drills L1 estate -> L2 tower ->
# L3 application entirely client-side from this one document: a few hundred
# applications and interfaces come to tens of KB, so per-click round-trips
# would cost more than they save.
# ---------------------------------------------------------------------------

def _group_by_app(rows):
    """Rows carrying `application_id` -> {app_id: [row, ...]}."""
    grouped = {}
    for row in rows:
        grouped.setdefault(row.application_id, []).append(row)
    return grouped


def build_estate_graph(db_session):
    """Assemble the full layered-view graph (see the module docstring).

    Returns a JSON-serialisable dict:
      generated_at : ISO-8601 UTC stamp
      counts       : {towers, apps, links, externals}
      towers       : [{id, name, lead, bc_mix, app_ids}]
      categories   : business-area domains (the Enterprise Architect view)
      apps         : [{id, tower_id, category_id, category, name, bc, summary,
                       servers, interfaces, support, vendors, tech,
                       users_count}]
      links        : resolved application-to-application edges, each
                     carrying every wording its records used for the channel
                     (`connectivity` is the first of them, `connectivities`
                     all of them)
      tower_links  : those links aggregated per unordered tower pair (L1)
      category_links : the same links aggregated per unordered domain pair
      externals    : interfaces that matched nothing in the estate
      warnings     : data-quality notes (duplicate hostnames, and connections
                     described by more than one interface record)

    One connection between two applications over one CHANNEL is ONE entry
    in `links`, however many interface rows declare it and however those
    rows word it - see the "One connection, one line" section above.
    """
    utility_names = _load_utility_names(db_session)

    towers = (
        db_session.query(AppSupportTower)
        .filter_by(is_active=True)
        .order_by(AppSupportTower.tower_name.asc())
        .all()
    )
    active_tower_ids = {t.id for t in towers}
    # Read once and indexed here rather than through each application's
    # `category` relationship, which would be one lazy SELECT per row.
    category_rows = _load_category_rows(db_session)
    category_name_by_id = {row[0]: (row[1] or '').strip()
                           for row in category_rows}
    apps = [
        a for a in (
            db_session.query(ApplicationDetails)
            .filter_by(is_active=True)
            .order_by(ApplicationDetails.application_name.asc())
            .all()
        )
        if a.tower_id in active_tower_ids
    ]
    app_ids = {a.id for a in apps}

    # Child records in bulk, grouped in memory: five queries for the whole
    # estate instead of five per application.
    def _children(model_cls):
        if not app_ids:
            return {}
        return _group_by_app(
            db_session.query(model_cls)
            .filter(model_cls.application_id.in_(app_ids))
            .all()
        )

    servers_by_app = _children(ServerDetails)
    interfaces_by_app = _children(ExternalInterfaces)
    support_by_app = _children(SupportTeam)
    vendors_by_app = _children(VendorDetails)
    users_by_app = _children(BusinessUsers)
    tech_by_app = _load_tech_rows(db_session, app_ids)

    app_name_by_id = {a.id: (a.application_name or '') for a in apps}
    tower_of_app = {a.id: a.tower_id for a in apps}

    name_index = build_name_index([(a.id, a.application_name) for a in apps])
    host_registry, warnings = build_host_registry([
        (app_id, app_name_by_id.get(app_id, ''), s.id, s.server_name)
        for app_id, rows in servers_by_app.items() for s in rows
    ])

    # ----- applications, with every interface resolved ---------------------
    app_payload = []
    directed = {}       # (src, dst, channel) -> link record
    bidir_keys = set()  # (frozenset(pair), channel)
    externals = {}

    for a in apps:
        interfaces = []
        for itf in interfaces_by_app.get(a.id, []):
            # The wording is what the record says; the channel is what it
            # MEANS, and only the channel identifies a connection.
            wording = (itf.connectivity_type or '').strip()
            connectivity = wording or INTERFACE_FALLBACK
            channel = channel_key(wording)
            direction = _enum_value(itf.flow_direction,
                                    FlowDirection.OUTBOUND.value)
            resolved = resolve_interface(itf.interface_name,
                                         itf.external_server, a.id,
                                         host_registry, name_index,
                                         utility_names)
            target = resolved['app_id']
            interfaces.append({
                'id': itf.id,
                'name': resolved['display'],
                'host': (itf.external_server or '').strip(),
                'connectivity': connectivity,
                'direction': direction,
                'resolved_app_id': target,
                'match': resolved['match'],
                'external': target is None,
            })

            if target is None:
                key = _norm(resolved['display']) or f'ext{itf.id}'
                node = externals.setdefault(
                    key, {'key': key, 'name': resolved['display'], 'links': []})
                _record_external(node, a.id, connectivity, channel, direction,
                                 itf.id)
                continue

            # Direction follows the interface's owner. Bi-directional
            # interfaces collapse into ONE edge rather than two opposed ones.
            if direction == FlowDirection.INBOUND.value:
                src, dst = target, a.id
            else:
                src, dst = a.id, target
            edge_key = (src, dst, channel)
            rec = directed.get(edge_key)
            if rec is None:
                rec = {
                    'src_app': src, 'dst_app': dst,
                    'channel': channel,
                    'direction': direction,
                    'via_host': resolved['via_host'],
                    'match': resolved['match'],
                    'hosts': [], 'interface_ids': [], 'declared_by': [],
                    'connectivities': [],
                }
                directed[edge_key] = rec
            _merge_declaration(rec, a.id, itf.id, resolved, wording)
            if direction == FlowDirection.BIDIRECTIONAL.value:
                bidir_keys.add((frozenset((src, dst)), channel))

        app_payload.append({
            'id': a.id,
            'tower_id': a.tower_id,
            'category_id': a.category_id,
            'category': category_name_by_id.get(a.category_id, ''),
            'name': a.application_name or f'App {a.id}',
            'bc': _enum_value(a.business_criticality, ''),
            'summary': a.application_summary or '',
            'servers': [{
                'id': s.id,
                'hostname': (s.server_name or '').strip(),
                'location': s.location or '',
                'os': s.operating_system or '',
                'env': s.environment_description or '',
            } for s in servers_by_app.get(a.id, [])],
            'interfaces': interfaces,
            'support': [{
                'name': m.member_name or '',
                'role': _enum_value(m.role, ''),
                'email': m.email or '',
            } for m in support_by_app.get(a.id, [])],
            'vendors': [{
                'name': v.vendor_name or '',
                'bespoke': bool(v.is_bespoke),
            } for v in vendors_by_app.get(a.id, [])],
            'tech': tech_by_app.get(a.id, []),
            'users_count': len(users_by_app.get(a.id, [])),
        })

    # ----- collapse opposed pairs into single bidirectional edges ----------
    #
    # One pair of applications joined one way is ONE line, however many
    # interface rows describe it. The opposite record, the reciprocal record
    # the other owner typed, and the second hostname the same integration
    # happens to land on are all folded into that one edge (see
    # _merge_declaration), and the rows that produced it travel on it as
    # `interface_ids` so a reader can still find every record behind a line.
    fold_unrecorded_channels(directed, bidir_keys)

    links, seen = [], set()
    for (src, dst, channel), rec in sorted(
            directed.items(),
            key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]))):
        pair_key = (frozenset((src, dst)), channel)
        if pair_key in seen:
            continue
        opposite = directed.get((dst, src, channel))
        is_bidir = (pair_key in bidir_keys or opposite is not None)
        seen.add(pair_key)
        if opposite is not None:
            rec = _fold_opposite(rec, opposite)
        # Direction is restated for the MERGED edge rather than kept from
        # whichever record happened to be read first: on a collapsed edge the
        # declaring row's own wording no longer says anything, because the
        # rows on both sides of the same wire disagree about it by design.
        #
        # The label is the FIRST wording the estate recorded for the channel,
        # with every other wording carried beside it: one line cannot be
        # captioned twice, and the words the other end used are worth keeping
        # rather than resolving away.
        wordings = rec['connectivities'] or [INTERFACE_FALLBACK]
        links.append(dict(rec, bidir=is_bidir,
                          direction=(FlowDirection.BIDIRECTIONAL.value
                                     if is_bidir
                                     else FlowDirection.OUTBOUND.value),
                          connectivity=wordings[0],
                          connectivities=list(wordings),
                          declarations=len(rec['interface_ids'])))

    # ----- L1 aggregation: one bundled edge per tower pair ------------------
    pair_counts = {}
    for link in links:
        ta = tower_of_app.get(link['src_app'])
        tb = tower_of_app.get(link['dst_app'])
        if ta is None or tb is None or ta == tb:
            continue        # intra-tower links belong to L2, not the estate ring
        key = (min(ta, tb), max(ta, tb))
        pair_counts[key] = pair_counts.get(key, 0) + 1
    tower_links = [{'a': a, 'b': b, 'count': n}
                   for (a, b), n in sorted(pair_counts.items())]

    # ----- towers ----------------------------------------------------------
    apps_by_tower = {}
    for entry in app_payload:
        apps_by_tower.setdefault(entry['tower_id'], []).append(entry)

    tower_payload = []
    for t in towers:
        members = apps_by_tower.get(t.id, [])
        bc_mix = {'BC1': 0, 'BC2': 0, 'BC3': 0}
        for entry in members:
            if entry['bc'] in bc_mix:
                bc_mix[entry['bc']] += 1
        tower_payload.append({
            'id': t.id,
            'name': t.tower_name or f'Tower {t.id}',
            'lead': t.tower_lead or '',
            'bc_mix': bc_mix,
            'app_ids': [entry['id'] for entry in members],
        })

    # ----- the same estate, sliced by business area ------------------------
    categories, category_links = build_category_view(app_payload, links,
                                                     category_rows)

    ext_list = sorted(externals.values(), key=lambda e: e['name'].lower())
    # Duplicate RECORDS are reported beside duplicate hostnames rather than
    # drawn: the edge above is already one line, and the estate's copy of the
    # truth is what needs the edit.
    warnings = warnings + collect_duplicate_declarations(
        links, ext_list, app_name_by_id)
    return {
        'generated_at': datetime.now(timezone.utc)
                                .strftime('%Y-%m-%dT%H:%M:%SZ'),
        'counts': {
            'towers': len(tower_payload),
            'apps': len(app_payload),
            'links': len(links),
            'externals': len(ext_list),
            # Interface ROWS behind those links, so a reader can see how much
            # of the record is the same connection described twice.
            'declarations': sum(len(l.get('interface_ids') or [])
                                for l in links),
        },
        'towers': tower_payload,
        'categories': categories,
        'apps': app_payload,
        'links': links,
        'tower_links': tower_links,
        'category_links': category_links,
        'externals': ext_list,
        'warnings': warnings,
    }
