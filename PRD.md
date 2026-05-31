# Designs:

## Main Notebook Page

![[all arial.png]]

## Projection Page:

![[aaa projection page.png]]

## Info-dense overview of the product:

Essential idea: Take notebooklm and improve it for STEM students (for me) make a power user tool

### notebook page

High level overview of the features:
regular notebooklm overview with sources, chat and outputs (both sources and outputs fully collapsible to focus on chat)

additions: secondary chat, and nested chatting (like reddit comment space) -> focus first on nested chatting it solves most of the needs for a secondary chat
-> Use Postgres’ built-in **ltree extension** for this

lower level overview of the features on this page:

the sources get chunked in the best possible way and then get ragged, though not sure whether pure rag is enough, need to support answering of questions like: "generate detailed summary from the selected sources" (the embedding vector of this question is not similar to any of the topics like e.g. _self attention_ maybe build agentic layer here)

### slash commands:

_/deepsummary_ : this command recursively breaks down topics and makes separate explanation sections for each of the subtopics then puts it back together for incredibly detailed explanation.

_/mockexam_ : preprompt to answer in specific structure, e.g. delimiting answers with <<<>>> and then hide it in collapsible sections inside the chat, essentially ability to generate nice exams with multiple choice and open questions and everything

_/voicerecall_ : active recall where you can record yourself and then the llm corrects what you said: can run this whenever for example after an exam or anything, instead of needing to manually work through the exam.
-> use openai whisper or alternative provider like elevenlabs (i prefer elevenlabs)
->actually lets use deepgram, because it offers adding custom vocabulary which we could custom add based on the users sources

obvious feature: need for katex / latex support, whatever llms are best at writing already.

outputs and functions in the right section: quiz, anki flashcards to apkg export, but also txt source download, yeah that's mostly it

ability to view tokens used and see percentage of context window filled up and cost of current session

ability to have multiple chats, WHY doesn't notebooklm have this already

ability to while being in the same chat do message without the previous chat context, but then again do it with, so then the message gets appended to the previous messages for future chatting

### Projection Page:

be able to track what has been passively studied / through summaries or questioning the llm

or actively studied through quiz or mock exam

maybe also ability to view it in 3d not only 2d, yes but default in 2d

ability to search in the chunks for the precise term, like similarity and or keyword search using BM25

projection algorithms and how to implement it:
UMAP, PCA and t-SNE (t-distributed Stochastic Neighbour Embedding)

HOW TO BUILD IT:

1. When a document finishes chunking and embedding, your ARQ background worker runs it through umap-learn (both 2D and 3D), scikit-learn PCA, and scikit-learn t-SNE it does this with ALL the chunks, the old and the new
2. The worker saves the resulting x, y (and z) coordinates for each algorithm into the Postgres chunks table as JSON.
3. The SvelteKit frontend just fetches these pre-calculated coordinates.
4. when some chunk is used for passively, or actively studying it can then be marked in the db

### some technical aspects:

be able to set topk, topP and temperature for the models
per default: byok via openrouter
but also support for ollama local llms.
ability to plug in mcp servers and skills
full transparency of system prompts and slash command preprompts

ability to change fonts for the user: chose from default arial, inter and some others

### vision:

moldable ui and moldable functionalities via ai coding

build up mental model of user over time, to understand how you keep concepts in your brain, and be able to quicker tailor to your world view and explain things by tieing them to things you already understand (dont know how, but would be nice to have)
-> but this is really for the future, starting to inject context across notebooks becomes messy and pollutes the context

### techstack:

SvelteKit + Tailwind + KaTeX frontend
python backend with fastapi
some vectordb hosted on railway (everything gets hosted on railway)

regular db being postgres, mostly non sql except for user authentication, maybe use postgres addon for vectordb? but maybe not if we want to be able to do cool advanced vector stuff

authentication using jwt tokens and this flow.

for parsing the documents:
LLamaParse api or similar so that it works great with math and complex visualizations
Use Postgres’ built-in **ltree extension**
use `asyncpg` and alembic just for schema versioning
Keep pgvector. Create **HNSW indexes** on your embedding columns. It scales to millions of vectors with millisecond latency.

 task queue for three things: PDF ingestion, generating .apkg files, and recalculating the UMAP projection. Since you are using Python (FastAPI), use **ARQ** (Async Redis Queue) or **Celery**.

UMAP is CPU-heavy. Do not run it on your main FastAPI web server. Have a dedicated background worker pick up the UMAP job, calculate the 2D coordinates, save them to the Postgres DB as JSON, and then the frontend just fetches the pre-calculated coordinates.
->maybe overkill for a first version

If the query is global, bypass vector search. Pre-compute and store a "Document Summary" during ingestion, and inject that into the context window instead -> or build agentic system and the agent explores those documents as it likes, maybe multiple nested clusters???

integrate langsmith for observability

**File Storage:** Cloudflare R2 (AWS S3 compatible, zero egress fees) for storing uploaded PDFs and generated .apkg files. -> yeah need to think of how to solve this

### addons:

browser extension as mcp that can be connected to claude to be able to query your vectordb from within claude. (the extension already exists just need to build in account sync)
