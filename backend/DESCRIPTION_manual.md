## Backend:

Has 2 main elements:

- the main.py web api endpoint
- and the ARQ worker

### Main.py webserver

it mounts the APIRouter objects which live inside **auth/auth.py** and inside **library/documents.py** and **library/project.py**

- auth/auth.py just provides some authentication endpoints like /register, /login, /logout and /me
- library/project.py just has some endpoints for creating new projects using _create_project_ POST endpoint and _list_projects_ a get endpoint
- library/documents.py is where the main action start happening

#### library/documents.py:

endpoints exposed by it: upload_document, upload_documents, list_documents

now when upload_document or upload_documents are called the pdf gets first \_store_and_enqueue'd
this function stores the pdf: storage.upload_bytes() and inserts into the documents table that we have this new document
then it enqueues the ingest: enqueue_ingest()

### enqueue_ingest() and producer.py

this is the production side of the ARQ worker, this is where new jobs get enqueued
now enqueue_ingest() enqueues the -> ingest_document job for the worker

### worker.py and ingest_document

the worker is the one that reads from the job queue and processes the tasks

ingest_document given one document:

1. parses it
2. chunks it
3. embeds the chunks
4. writes the new chunks to the chunks table -> (this is currently idempotent, meaning it deletes for a doc_id all previous chunks and replaces them with new)

### Finish of library/documents.upload_documents()

so then after the chunks have been created we still need to create the UMAP 2d, 3d and t-SNE and PCA projection and clustering
-> since this might take a while, it is not awaited before writing the chunks to db
-> this is another job that is enqueued, namely: enqueue_finalize() -> finalize_project (workers job function)

the finalize_project function in turn enqueues recalculate_projections and recalculate_clusters on the pid (project id)

NOTE: it would be nice if further upload_document(s) calls come in, e.g. first 1 doc is uploaded then another one, that it overtakes the first processes enqueue_finalize
to not need to recompute the projections and clustering twice
-> not sure how to do this though

## chat/chat.py

the actual endpoint which allows users to ask questions and get responses grounded in their data
it uses the route() from retrieval/retrieval_router.py to decide which strategy to use for retrieval and then outputs the retrieved content
-> this retrieved content gets then sent with the correct history (since chat is nested it needs to figure this out from the ltree) and user msg to the llm

### preferences/preferences.py

one unified place where small settings, such as topk, topp, max_tokens, top k retrieval, temperature etc are set, which can be changed by the user, since everythign needs to be controllable.

General open questions:
max_tokens, topk and params like this need to be transparent and controllable

then also \_classify_intent and \_classify_scope needs to be done via llm preferably not via hardcoded couple of words

also add spacy for chunk context compression

not every retrieved chunk is used, make the llm decide which chunks it uses and only add those to citations

in deepsummary, make the first pass not retrieve all chunks, but only based on the ltree dictionary maybe decide what to write? not sure, i mean where it sees the global thing is good, but at the same time it burns tokens
