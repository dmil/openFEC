import logging
import re
from collections import defaultdict
from urllib.parse import urlencode

from webservices.env import env
from webservices.rest import db
from webservices.utils import get_elasticsearch_connection
from webservices.tasks.utils import get_bucket
from webservices.reclassify_statutory_citation import reclassify_pre2012_citation

logger = logging.getLogger(__name__)

ALL_MURS = """
    SELECT case_id, case_no, name
    FROM fecmur.case
    WHERE case_type = 'MUR'
"""

MUR_SUBJECTS = """
    SELECT subject.description AS subj, relatedsubject.description AS rel
    FROM fecmur.case_subject
    JOIN fecmur.subject USING (subject_id)
    LEFT OUTER JOIN fecmur.relatedsubject USING (subject_id, relatedsubject_id)
    WHERE case_id = %s
"""

MUR_PARTICIPANTS = """
    SELECT entity_id, name, role.description AS role
    FROM fecmur.players
    JOIN fecmur.role USING (role_id)
    JOIN fecmur.entity USING (entity_id)
    WHERE case_id = %s
"""

MUR_DOCUMENTS = """
    SELECT document_id, category, description, ocrtext,
        fileimage, length(fileimage) AS length,
        doc_order_id, document_date
    FROM fecmur.document
    WHERE case_id = %s
    ORDER BY doc_order_id, document_date desc, document_id DESC;
"""
# TODO: Check if document order matters

MUR_VIOLATIONS = """
    SELECT entity_id, stage, statutory_citation, regulatory_citation
    FROM fecmur.violations
    WHERE case_id = %s
    ;
"""

STATUTE_REGEX = re.compile(r'(?<!\()(?P<section>\d+([a-z](-1)?)?)')
REGULATION_REGEX = re.compile(r'(?<!\()(?P<part>\d+)(\.(?P<section>\d+))*')

def load_current_murs():
    es = get_elasticsearch_connection()
    bucket = get_bucket()
    bucket_name = env.get_credential('bucket')
    with db.engine.connect() as conn:
        rs = conn.execute(ALL_MURS)
        for row in rs:
            case_id = row['case_id']
            mur = {
                'doc_id': 'mur_%s' % row['case_no'],
                'no': row['case_no'],
                'name': row['name'],
                'mur_type': 'current',
            }
            mur['subject'] = {"text": get_subjects(case_id)}

            participants = get_participants(case_id)
            assign_citations(participants, case_id)
            mur['participants'] = list(participants.values())

            mur['text'], mur['documents'] = get_documents(case_id, bucket, bucket_name)
            # TODO pdf_pages, open_date, close_date, url
            es.index('docs', 'murs', mur, id=mur['doc_id'])

def get_participants(case_id):
    participants = {}
    with db.engine.connect() as conn:
        rs = conn.execute(MUR_PARTICIPANTS, case_id)
        for row in rs:
            participants[row['entity_id']] = {
                'name': row['name'],
                'role': row['role'],
                'citations': defaultdict(list)
            }
    return participants

def get_subjects(case_id):
    subjects = []
    with db.engine.connect() as conn:
        rs = conn.execute(MUR_SUBJECTS, case_id)
        for row in rs:
            if row['rel']:
                subject_str = row['subj'] + "-" + row['rel']
            else:
                subject_str = row['subj']
            subjects.append(subject_str)
    return subjects

def assign_citations(participants, case_id):
    with db.engine.connect() as conn:
        rs = conn.execute(MUR_VIOLATIONS, case_id)
        for row in rs:
            entity_id = row['entity_id']
            if entity_id not in participants:
                logger.warn("Entity %s from violations not found in participants for case %s", entity_id, case_id)
                continue
            participants[entity_id]['citations'][row['stage']].extend(
                parse_statutory_citations(row['statutory_citation'], case_id, entity_id))
            participants[entity_id]['citations'][row['stage']].extend(
                parse_regulatory_citations(row['regulatory_citation'], case_id, entity_id))

def parse_statutory_citations(statutory_citation, case_id, entity_id):
    citations = []
    if statutory_citation:
        for match in STATUTE_REGEX.finditer(statutory_citation):
            title, section = reclassify_pre2012_citation('2', match.group('section'))
            url = 'https://api.fdsys.gov/link?' +\
                urlencode([
                    ('collection', 'uscode'),
                    ('year', 'mostrecent'),
                    ('link-type', 'html'),
                    ('title', title),
                    ('section', section)
                ])
            citations.append(url)
        if not citations:
            logger.warn("Cannot parse statutory citation %s for Entity %s in case %s",
                statutory_citation, entity_id, case_id)
    return citations

def parse_regulatory_citations(regulatory_citation, case_id, entity_id):
    citations = []
    if regulatory_citation:
        for match in REGULATION_REGEX.finditer(regulatory_citation):
            url = 'https://api.fdsys.gov/link?' +\
                urlencode([
                    ('collection', 'cfr'),
                    ('year', 'mostrecent'),
                    ('titlenum', '11'),
                    ('partnum', match.group('part'))
                ])
            if match.group('section'):
                url += '&' + urlencode([('sectionnum', match.group('section'))])
            citations.append(url)
        if not citations:
            logger.warn("Cannot parse regulatory citation %s for Entity %s in case %s",
                regulatory_citation, entity_id, case_id)
    return citations

def get_documents(case_id, bucket, bucket_name):
    documents = []
    document_text = ""
    with db.engine.connect() as conn:
        rs = conn.execute(MUR_DOCUMENTS, case_id)
        for row in rs:
            document = {
                'document_id': row['document_id'],
                'category': row['category'],
                'description': row['description'],
                'length': row['length'],
                'document_date': row['document_date'],
            }
            document_text += row['ocrtext'] + ' '
            pdf_key = 'legal/murs/current/%s.pdf' % row['document_id']
            logger.info("S3: Uploading {}".format(pdf_key))
            bucket.put_object(Key=pdf_key, Body=bytes(row['fileimage']),
                              ContentType='application/pdf', ACL='public-read')
            document['url'] = "https://%s.s3.amazonaws.com/%s" % (bucket_name, pdf_key)
            documents.append(document)
    return document_text, documents
