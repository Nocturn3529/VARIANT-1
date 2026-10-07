"""Outcome-level mission, evolving task resources and independent artifact checks."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.request
from urllib.parse import urlparse

PROJECTS = (
    {'project':'cpython','url':'https://github.com/python/cpython','license':'PSF-2.0'},
    {'project':'sqlite','url':'https://sqlite.org','license':'public-domain'},
    {'project':'git','url':'https://git-scm.com','license':'GPL-2.0'},
)


def publish_feed(project, cycle):
    root = Path(project)/'inputs'
    root.mkdir(parents=True, exist_ok=True)
    records = [{**item,'revision':cycle,'status':'review-required' if index == cycle % len(PROJECTS) else 'available',
                'source_id':f'status-{cycle}-{index}'} for index,item in enumerate(PROJECTS)]
    path = root/f'status-{cycle:06d}.json'
    payload = json.dumps({'schema':'observatory.status-feed.v1','revision':cycle,'records':records},indent=2)+'\n'
    if path.exists():
        if path.read_text(encoding='utf-8') != payload:
            raise RuntimeError('Existing feed differs from its deterministic revision')
    else:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(payload,encoding='utf-8')
        temporary.replace(path)
    return records


def mission(project, peers, deadline):
    until = datetime.fromtimestamp(deadline, timezone.utc).isoformat()
    return (
        f'Build and operate a Research Observatory in {Path(project).resolve()} through {until}. '
        'Create a useful searchable dashboard and a sourced research report about the public software projects '
        'listed in inputs/. Research their architecture and development practices from primary sources. '
        'The changing status feeds in inputs/ are task resources: retain their history, apply the latest revision, '
        'identify corrections and keep the dashboard and reports current throughout the observation window. '
        'You own this Goal and are responsible for coordinating, reviewing and integrating the team\'s work. '
        f'The human authorizes you and these independent sessions to collaborate in this shared project: {json.dumps(peers)}. '
        'Collaborators are available for your requests; you decide their work and may explicitly delegate additional children. '
        'Keep all mission changes in this project. Deliver a running local web application, research-index.json '
        '(an array of records with project, url, license, status, revision and source_id), a cited report.md, '
        'and service.json containing the dashboard health_url. Include meaningful executable tests, documented startup '
        'and operating records with timestamps. Work autonomously across the full window. Completion requires verified '
        'deliverables and operating evidence covering that window. Report uncertainty and required input honestly.'
    )


def grade(project, expected, *, now, deadline):
    root = Path(project)
    checks = {'observation_window_complete':now >= deadline}
    errors = []
    try:
        rows = json.loads((root/'research-index.json').read_text(encoding='utf-8'))
        fields = ('project','url','license','status','revision','source_id')
        if not isinstance(rows,list) or len(rows) != len(expected):
            raise ValueError('Index must contain one current record per project')
        actual = {row['project']:row for row in rows if isinstance(row,dict)}
        checks['exact_latest_feed'] = len(actual) == len(expected) and all(
            all(actual.get(row['project'],{}).get(key) == row[key] for key in fields) for row in expected)
    except (OSError,ValueError,KeyError,TypeError):
        checks['exact_latest_feed'] = False
    try:
        report = (root/'report.md').read_text(encoding='utf-8')
        checks['source_links_present'] = all(row['url'] in report for row in expected)
    except OSError:
        checks['source_links_present'] = False
    try:
        url = json.loads((root/'service.json').read_text(encoding='utf-8'))['health_url']
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password:
            raise ValueError('Health URL must be loopback HTTP')
        # Do not follow redirects to arbitrary hosts while grading an agent artifact.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):
                return None
        with urllib.request.build_opener(NoRedirect).open(url,timeout=3) as response:
            checks['running_dashboard'] = response.status == 200 and bool(response.read(1024))
    except (OSError,ValueError,KeyError,TypeError):
        checks['running_dashboard'] = False
    return {'checks':checks,'passed':all(checks.values()),'errors':errors,
            'limitations':'Exact feed and health are independently checked. Citation validity, test quality, browser interaction and operating evidence still require review; artifact checks alone do not certify the full mission.'}
