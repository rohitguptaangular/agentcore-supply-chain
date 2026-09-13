"""Knowledge base specialist agent.

A deliberately small agent with one job: answer a question from the company's
written documents, and say where the answer came from.

It is written without an agent framework, because it does not need one. There
is no tool selection to make and no multi-step plan to form — the work is
retrieve, then summarise. Two Bedrock calls and some string handling. Adding a
reasoning loop here would cost tokens and latency for no capability.

That contrast is the point of splitting the system into two agents: the
orchestrator decides *what to do*, this one just *does one thing well* and runs
on a model an order of magnitude cheaper.

RAG in three steps, all visible below:
    retrieve  — Bedrock embeds the question and returns the closest passages
    ground    — those passages become the only material the model may use
    generate  — the model answers and cites the documents
"""

from __future__ import annotations

import logging
import os

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOG = logging.getLogger("kb_specialist")

app = BedrockAgentCoreApp()

MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-lite-v1:0")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")

# How many passages to retrieve. More passages means better recall and a larger
# prompt; five is enough for procedural documents where the answer is usually
# contained in one or two chunks.
PASSAGE_COUNT = 5

# bedrock-agent-runtime is the data plane for knowledge bases: Retrieve and
# RetrieveAndGenerate live here, separate from the control plane that creates
# them and from bedrock-runtime which invokes models.
_agent_runtime = boto3.client("bedrock-agent-runtime")
_bedrock = boto3.client("bedrock-runtime")

ANSWER_PROMPT = """\
You answer questions about company procedures using only the passages provided.

Rules:
- Use only what the passages say. If they do not answer the question, say so.
- Cite the source document for each fact, using the labels given.
- Do not add best practice, general knowledge or plausible-sounding detail.
- Keep the answer tight. Use numbered steps when the passages describe a process.

Question:
{question}

Passages:
{passages}
"""


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    """Answer one document question."""
    question = (payload or {}).get("prompt", "").strip()
    if not question:
        return {"result": "No question was provided."}

    if not KNOWLEDGE_BASE_ID:
        return {"result": "No knowledge base is configured for this deployment."}

    LOG.info("Retrieving for: %r", question)
    passages = _retrieve(question)

    if not passages:
        # Explicitly reporting an empty retrieval is important: it lets the
        # orchestrator tell the user the documents do not cover this, instead
        # of a model inventing an answer from nothing.
        return {
            "result": (
                "The company documents contain nothing relevant to that question."
            ),
            "passages_found": 0,
        }

    answer = _generate(question, passages)

    return {
        "result": answer,
        "passages_found": len(passages),
        "sources": sorted({p["source"] for p in passages}),
    }


def _retrieve(question: str) -> list[dict]:
    """Semantic search over the knowledge base.

    Bedrock embeds the question with the same model used to embed the
    documents, then returns the nearest chunks. This is what makes "receiving
    procedure" match a document that says "goods inwards process" — the match
    is on meaning, not on words.
    """
    response = _agent_runtime.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": question},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": PASSAGE_COUNT}
        },
    )

    passages = []
    for result in response.get("retrievalResults", []):
        text = result.get("content", {}).get("text", "")
        if not text.strip():
            continue

        location = result.get("location", {})
        uri = location.get("s3Location", {}).get("uri", "unknown source")

        passages.append(
            {
                # Just the filename — a full S3 URI in a citation is noise to
                # the reader and wasted tokens in the prompt.
                "source": uri.rsplit("/", 1)[-1] or uri,
                "text": text,
                "score": result.get("score", 0),
            }
        )

    LOG.info("Retrieved %s passages", len(passages))
    return passages


def _generate(question: str, passages: list[dict]) -> str:
    """Summarise the retrieved passages into a cited answer."""
    formatted = "\n\n".join(
        f"[{index}] Source: {passage['source']}\n{passage['text']}"
        for index, passage in enumerate(passages, start=1)
    )

    response = _bedrock.converse(
        modelId=MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": ANSWER_PROMPT.format(
                            question=question, passages=formatted
                        )
                    }
                ],
            }
        ],
        inferenceConfig={
            # Near-zero: this agent must not be creative. Its entire job is
            # faithful restatement of the retrieved text.
            "temperature": 0.0,
            "maxTokens": 1024,
        },
    )

    return response["output"]["message"]["content"][0]["text"]


if __name__ == "__main__":
    app.run()
