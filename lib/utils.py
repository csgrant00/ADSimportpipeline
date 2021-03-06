import os,sys
import pymongo
import time
import itertools
import json
import copy
import uuid

import settings
from rules import merger
from lib import xmltodict
from lib import collections
try:
  import cPickle as pickle
except ImportError:
  import pickle
try:
  from ads.ADSExports import ADSRecords
except ImportError:
  sys.path.append('/proj/ads/soft/python/lib/site-packages')
  try:
    from ads.ADSExports import ADSRecords
  except ImportError:
    print "Unable to import ads.ADSExports.ADSRecords!"
    print "We will be unable to query the ADS system for records!"

#This is not in settings because it normalizes the XML schema coming directly from ADSExports;
#Changing this will break the merger logic, as it expects a consistent schema.
NORMALIZE_SCHEMA = {
  'arxivcategories':  lambda d: ensureList(d),
  'author':           lambda d: ensureList(d),
  'keywords':         lambda d: ensureList(d),
  'title':            lambda d: ensureLanguageSchema(d),
  'abstract':         lambda d: ensureLanguageSchema(d),
  'dates':            lambda d: ensureList(d),
  'isbns':            lambda d: ensureList(d),
  'issns':            lambda d: ensureList(d),
  'databases':        lambda d: ensureList(d),
  'bibgroups':        lambda d: ensureList(d),
  'reference':        lambda d: ensureList(d),
  'alternates':     lambda d: ensureList(d),
  'associates':       lambda d: ensureList(d),
  'links':            lambda d: ensureList(d),
}

def ensureLanguageSchema(item):
  if isinstance(item,basestring):
    L = [{
      '@lang':'en',
      '#text': item
    }]
  else:
    L = ensureList(item)
    for i in L:
      if '@lang' not in i:
        i['@lang'] = 'en'
  return L

def ensureList(item):
  return item if isinstance(item,list) else [item]

def init_db(db,LOGGER,MONGO):
  db[MONGO['COLLECTION']].ensure_index('bibcode',unique=True)

def mongoCommit(records,LOGGER=settings.LOGGER,MONGO=settings.MONGO):
  '''
  Commits records(@type dict) to a mongo
  '''
  if not records:
    return False
  conn = pymongo.MongoClient(host=MONGO['MONGO_URI'])
  db = conn[MONGO['DATABASE']]
  if MONGO['COLLECTION'] not in db.collection_names():
    init_db(db,LOGGER,MONGO)
  collection = db[MONGO['COLLECTION']]
  for r in records:
    assert(r['bibcode'])
    assert(r['JSON_fingerprint'])
    assert(r['metadata'])
    #query = {"bibcode": {"$in": [r['bibcode'] for r in records]}}
    query = {"bibcode": r['bibcode']}
    collection.update(query,r,upsert=True,w=1,multi=False) #w=1 means block all write requests until it has written to the primary
  conn.close()

def findChangedRecords(records,LOGGER=settings.LOGGER,MONGO=settings.MONGO):
  '''
  Finds records in mongodb that need updating.
  Update criteria: JSON_fingerprint field different from the input records

  records: [(bibcode,JSON_fingerprint),...]
  '''
  if not records:
    LOGGER.debug("No records given")
    return []

  conn = pymongo.MongoClient(host=MONGO['MONGO_URI'])
  db = conn[MONGO['DATABASE']]

  if MONGO['COLLECTION'] not in db.collection_names():
    init_db(db,LOGGER,MONGO)
  collection = db[MONGO['COLLECTION']]
  currentRecords = [(r['bibcode'],r['JSON_fingerprint']) for r in collection.find({"bibcode": {"$in": [rec[0] for rec in records]}})]
  conn.close()
  return list(set([(r[0],r[1]) for r in records]).difference(currentRecords))

def readRecordsFromFiles(records,files,LOGGER=settings.LOGGER):
  '''
  records: [(bibcode,JSON_fingerprint),...]
  '''

  if not records:
    LOGGER.debug("No records given")
    return []

  targets = dict(records)
  records = []

  for file_ in files:
    with open(file_) as fp:
      recs = pickle.load(fp)

  records.extend( [r for r in recs if r['@bibcode'] in targets] )
  return records,targets


def readRecords(records,LOGGER=settings.LOGGER):
  '''
  records: [(bibcode,JSON_fingerprint),...]
  '''
  h = hash(json.dumps(records))
  if not records:
    LOGGER.debug("No records given")
    return []

  targets = dict(records)

  s = time.time()
  records = ADSRecords('full','XML')
  failures = []
  for bibcode in targets.keys():
    try:
      records.addCompleteRecord(bibcode)
    except KeyboardInterrupt:
      raise
    except:
      failures.append(bibcode)
      LOGGER.warning("[%s] ADSRecords failed" % bibcode)
  records = records.export()
  if not records.content:
    return []
  ttc = time.time()-s
  rate = len(targets)/ttc
  if failures:
    LOGGER.warning('ADSRecords failed to retrieve %s records' % len(failures))
  LOGGER.info('ADSRecords took %0.1fs to query %s records (%0.1f rec/s)\t[%s]' % (ttc,len(targets),rate,h))

  records = ensureList(xmltodict.parse(records.__str__())['records']['record'])
  assert(len(records)==len(targets)-len(failures))

  # with open('%s.pickle' % uuid.uuid4(),'w') as fp:
  #   pickle.dump(records,fp)
  return records,targets

def updateRecords(records,targets,LOGGER=settings.LOGGER):

  #Could send these tasks out on a queue
  completeRecords = []
  for r in records:
    #Define top-level schema that will go in mongo
    cr = {
      'bibcode': r['@bibcode'],
      'JSON_fingerprint': targets[r['@bibcode']],
      'metadata' : {},
    }

    #Find metadata blocks that need merging
    metadataCounter = collections.Counter([entry['@type'] for entry in r['metadata']])
    needsMerging = dict([(k,[]) for k,v in metadataCounter.iteritems() if v>1])

    #Iterate over metadata blocks; directly input single defined blocks
    #and build a 'needsMerging' list to merge in the next step
    for metadataBlock in r['metadata']: 
      for field,data in metadataBlock.iteritems():
        if field not in ['@origin','modification_time','creation_time','@type']:
          metadataBlock[field] = {
            '@origin':metadataBlock['@origin'].upper(),
            'content':NORMALIZE_SCHEMA[field](data) if field in NORMALIZE_SCHEMA else data,
            'modtime':metadataBlock.get('modification_time',metadataBlock.get('creation_time',0))
          }
      if metadataBlock['@type'] not in needsMerging:
        cr['metadata'].update({metadataBlock['@type']:metadataBlock})
      else: #If it shows up more than once, it needs merging.
        needsMerging[metadataBlock['@type']].append(metadataBlock)
    #Now merge the multiply defined metadataBlocks
    for entryType,data in needsMerging.iteritems():
      cr['metadata'].update({entryType:merge(data,r['@bibcode'],entryType,LOGGER)})
    
    #Finally, we have a complete record
    completeRecords.append(enforceSchema(cr))

  LOGGER.info('Added %s complete records' % len(completeRecords))
  return completeRecords

def enforceSchema(record,LOGGER=settings.LOGGER):
  '''
  translates schema from ADSRecords to alternative schema
  '''

  #1. Delete records that we no longer want
  for deletion in settings.SCHEMA['deletions']:
    current_loc=record
    for key in deletion[:-1]:
      current_loc=current_loc[key]
    try:
      del current_loc[deletion[-1]]
    except KeyError:
       pass



  #Metadatablock "general"
  block='general'
  m='metadata'
  if 'electronic_id' in record[m][block]:
    for field in ['page','page_range']:
      if field in record[m][block]:
        del record[m][block][field]
  if 'page' in record[m][block] and 'page_range' in record[m][block] and record[m][block]['page'] == record[m][block]['page_range']:
    del record[m][block]['page_range']

  #  arxivcategories
  f = 'arxivcategories'
  record[m][block][f] = record[m][block].get(f,[])
  if record[m][block][f]:
    current = record[m][block][f]['content']
    res = []
    for i in current:
      content = i['arxivcategory']
      for j in ensureList(content):
        if isinstance(j,str):
          res.append(j)
        elif isinstance(j,dict):
          res.append(j['#text'])
    record[m][block][f] = res

  #  keywords
  f = 'keywords'
  record[m][block][f] = record[m][block].get(f,[])
  if record[m][block][f]:
    res = []
    for c in record[m][block][f].get('content',record[m][block][f]):
      content = c.get('content',c)
      origin = c['@origin'] if '@origin' in c else record[m][block][f]['@origin']
      type_ =  content['@type']
      for keyword in ensureList(content['keyword']):
        res.append({
          '@origin':origin,
          '@type': type_,
          'channel': keyword.get('channel',None),
          'original': keyword.get('original',None),
          'normalized': keyword.get('normalized',None),
          })
    record[m][block][f] = res

  for f in ['title','abstract']:
    record[m][block][f] = record[m][block].get(f,[])
    if record[m][block][f]:
      record[m][block][f] = record[m][block][f]['content']

  # authors
  f = 'author'
  record[m][block][f] = record[m][block].get(f,[])
  if record[m][block][f]:
    record[m][block][f] = record[m][block][f]['content']
    res = []
    for a in record[m][block][f]:
      orcid = ensureList(a.get('author_ids',[]))
      assert len(orcid)==1 or len(orcid)==0
      orcid = orcid[0]['author_id'].replace('ORCID:','') if orcid else None
      res.append( {
        '@nr': a['@nr'],
        'type': a.get('type',None),
        'affiliations': [i.get('affiliation',None) for i in ensureList(a.get('affiliations',[]))],
        'emails': [i['email'] for i in ensureList(a.get('emails',[]))],
        'orcid': orcid,
        'name': {
          'native': a['name'].get('native',None),
          'western': a['name'].get('western',None),
          'normalized': a['name'].get('normalized',None),
        },
      })
    record[m][block][f] = res

  # language
  f = 'language'
  record[m][block][f] = record[m][block].get(f,[])

  #  pages
  f = 'pages'
  subfields = ['pagenumber','page_range',{'lastpage':'page_last'},'page']
  record[m][block][f] = {}
  origins = []
  for sf in subfields:
    translation = sf
    if isinstance(sf,dict):
      sf,translation = sf.items()[0]
    res = record[m][block].get(sf,{})
    record[m][block][f][translation] = res.get('content',None)
    origins.append(res.get('@origin',None))
    try:
      del record[m][block][sf]
    except KeyError:
      pass
  try:
    record[m][block][f]['@origin'] = max(collections.Counter([i for i in origins if i]))
  except ValueError:
    record[m][block][f]['@origin'] = None

  # dates
  f = 'dates'
  record[m][block][f] = record[m][block].get(f,{})
  if record[m][block][f]:
    res = {}
    if 'publication_year' in record[m][block]:
      res['publication_year'] = {
        '@origin': record[m][block]['publication_year']['@origin'],
        'content': record[m][block]['publication_year']['content'],
      }
      del record[m][block]['publication_year']
    for c in ensureList(record[m][block][f].get('content',[])):
      res[c['date']['@type']] = {
        '@origin': c.get('@origin',record[m][block][f]['@origin']),
        'content': c['date']['#text'],
      }
    record[m][block][f] = res

  #  journal
  f = 'journal'
  subfields = ['volume','issue']
  raw = record[m][block].get(f,{}).get('content',None)
  record[m][block][f] = {}
  record[m][block][f]['name'] = {
    'raw': raw,
    'canonical': record[m][block].get('canonical_journal',{}).get('content',None),
  }
  try:
    del record[m][block]['canonical_journal']
  except KeyError:
    pass
  origins = []
  for sf in subfields:
    res = record[m][block].get(sf,None)
    record[m][block][f][sf] = None
    if res:
      record[m][block][f][sf] = res['content']
      origins.append(res['@origin'])
    try:
      del record[m][block][sf]
    except KeyError:
      pass
  try:
    record[m][block][f]['@origin'] = max(collections.Counter([i for i in origins if i]))
  except ValueError:
    record[m][block][f]['@origin'] = None

  # electronic_id, conf_metadata, DOI, copyright
  fields = ['electronic_id','conf_metadata','DOI','copyright']
  for f in fields:
    record[m][block][f.lower()] = record[m][block].get(f,{})
    try:
      del record[m][block][f]['modtime']
    except KeyError:
      pass

  # isbns, issns, objects
  fields = ['isbns','issns','objects']
  for f in fields:
    record[m][block][f] = record[m][block].get(f,[])
    if record[m][block][f]:
      res = []
      for c in ensureList(record[m][block][f].get('content',record[m][block][f])):
        res.append({
          '@origin':c.get('@origin',record[m][block][f]['@origin']),
          'content':c.get('content',c)[f[:-1]],
          })
      record[m][block][f] = res

  # instruments
  f = 'instruments'
  record[m][block][f] = record[m][block].get(f,[])
  if record[m][block][f]:
    res = []
    for c in ensureList(record[m][block][f]):
      res.append({
          '@origin':c.get('@origin',record[m][block][f]['@origin']),
          'content':c.get('content',c),
        })
    record[m][block][f] = res

  #Metadatablock "properties"
  block='properties'

  fields = ['databases','bibgroups']
  for f in fields:
    record[m][block][f] = record[m][block].get(f,[])
    if record[m][block][f]:
      res = []
      for c in record[m][block][f].get('content',record[m][block][f]):
        res.append({
          '@origin':c.get('@origin',record[m][block][f]['@origin']),
          'content':c.get('content',c)[f[:-1]],
          })
      record[m][block][f] = res

  f = 'pubtype'
  record[m][block][f] = record[m][block].get(f,{})
  if record[m][block][f]:
    record[m][block][f] = {
      '@origin':record[m][block][f]['@origin'],
      'content':record[m][block][f]['content'],
    }

  fields = ['openaccess','nonarticle','ocrabstract','private','refereed']
  for f in fields:
    record[m][block][f] = record[m][block].get(f,None)
    if record[m][block][f]:
      if record[m][block][f]['content']=="1":
        record[m][block][f] = True
      elif record[m][block][f]['content']=="0":
        record[m][block][f] = False
      else:
        record[m][block][f] = record[m][block][f]['content']

  block = 'references'
  record[m][block] = record[m].get(block,[])
  res = []
  if record[m][block]:
    for c in record[m][block]['reference']['content']:
      origin = c.get('@origin',record[m][block]['reference']['@origin'])
      if 'content' in c: #This happens in the case of certain merged cases
        c = c['content']
        origin = c.get('@origin',origin)
      res.append({
        '@origin':origin,
        'bibcode':c.get('@bibcode',None),
        'doi':c.get('@doi',None),
        'score':c.get('@score',None),
        'extension':c.get('@extension',None),
        'arxid': c.get('@arxid',None),
        'content': c.get('#text',None)
      })
    record[m][block] = res

  block='relations'
  f = 'preprintid'
  res = []
  record[m][block] = record[m].get(block,{})
  record[m][block][f] = record[m][block].get(f,[])
  if record[m][block][f]:
    c = []
    for i in ensureList(record[m][block][f]['content']):
      if i not in c:
        c.append(i)
    assert len(c) == 1
    c = c[0]
    origin = c.get('@origin',record[m][block][f]['@origin'])
    if 'content' in c: #This happens in the case of certain merged cases
      c = c['content']
      origin = c.get('@origin',origin)
    res = {
      '@origin':origin,
      '@ecode': c.get('@ecode',None),
      'content': c.get('#text',None)
    }
  record[m][block]['preprint'] = res
  del record[m][block][f]

  f = 'alternates'
  record[m][block][f] = record[m][block].get(f,[])
  res = []
  if record[m][block][f]:
    for c in ensureList(record[m][block][f]['content']):
      if not c:
        continue
      origin = c.get('@origin',record[m][block][f]['@origin'])
      if 'content' in c: #This happens in the case of certain merged cases
        c = c['content']
        origin = c.get('@origin',origin)
        for alt in ensureList(c['alternate']):
          res.append({
            '@origin':origin,
            '@type': alt.get('@type',None),
            'content': alt.get('#text',None)
          })
    record[m][block][f] = res

  f = 'associates'
  record[m][block][f] = record[m][block].get(f,[])
  res = []
  if record[m][block][f]:
    for c in ensureList(record[m][block][f]['content']):
      origin = c.get('@origin',record[m][block][f]['@origin'])
      if 'content' in c: #This happens in the case of certain merged cases
        c = c['content']
        origin = c.get('@origin',origin)
      res.append({
        '@origin': origin,
        'comment': c.get('comment',None),
        'content': c.get('#text',None),
      })
  record[m][block][f] = res

  f = 'links'
  record[m][block][f] = record[m][block].get(f,[])
  res = []
  if record[m][block][f]:
    for c in ensureList(record[m][block][f]['content']):
      if not c:
        continue
      origin = c.get('@origin',record[m][block][f]['@origin'])
      if 'content' in c: #This happens in the case of certain merged cases
        c = c['content']
        origin = c.get('@origin',origin)
      for ln in ensureList(c['link']):
        if ln.get('@type',None) == "ADSlink":
          continue
        res.append({
          '@origin':origin,
          '@type':ln.get('@type',None),
          'content': ln.get('@url',None)
        })
    record[m][block][f] = res


  #3. Unique based on key,value within lists of dicts:
  for block,fields in record[m].iteritems():
    if block=='references':
      continue
    for field,value in fields.iteritems():
      if not value:
        continue
      res = value
      if isinstance(value,list):
        if isinstance(value[0],list):
          res = list(set(value))
        elif isinstance(value[0],dict):
          res = []
          for c in value:
            if c not in res:
              res.append(c)
      record[m][block][field] = res

  return record

def merge(metadataBlocks,bibcode,entryType,LOGGER=settings.LOGGER):
  '''
  Merges multiply defined fields within a list of <metadata> blocks
  Returns a single (merged) <metadata> block
  '''
  fieldsHist = collections.Counter([i for i in list(itertools.chain(*metadataBlocks)) if not i.startswith('@')])
  singleDefinedFields = [k for k,v in fieldsHist.iteritems() if v==1]
  multipleDefinedFields = [k for k,v in fieldsHist.iteritems() if v>1]
  #LOGGER.debug('%s entries in [%s] (type: %s) need merging' % (len(multipleDefinedFields),bibcode,entryType))
  
  #Create intermediate data structure that lets us easily iterate over those fields that merging, and
  #store the necessary metadata for mergingfg
  fields = {}
  for block in metadataBlocks:
    for fieldName,data in block.iteritems():
      if fieldName not in multipleDefinedFields:
        continue
      if fieldName not in fields:
        fields[fieldName] = []
      fields[fieldName].append({
        '@origin':block['@origin'].upper(),
        'content':data['content'] if isinstance(data,dict) else data,
        'modtime':block.get('modification_time',block.get('creation_time',0)),
        '@primary': block.get('@primary',{}).get('content',"True"),
      })

  #Merge those fields that are multiply defined      
  mergedResults = {}
  for fieldName,data in fields.iteritems():
    result = None
    while len(data) > 0:
      f1 = data.pop()
      f2 = result if result else data.pop()
      result = merger.dispatcher(f1,f2,fieldName)
    mergedResults[fieldName] = result
  
  #Combine all the pieces into the complete <metadata> block
  completeBlock = {'@type':entryType,}
  singleDefined = dict([(k,v) for block in metadataBlocks for k,v in block.iteritems() if k in singleDefinedFields])
  completeBlock.update(singleDefined)
  completeBlock.update(mergedResults)

  return completeBlock
