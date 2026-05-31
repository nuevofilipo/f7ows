## Language Models for Law and Social Science
9. LLM Alignment

## How did we get to LLMs and AI?
A brief history

## Data: Storage and Bandwidth
- Key data technologies from the 1900s:
- hard disks for efficient storage
- relational database systems for efficient access and linking
- the internet – for sharing, and then for collection
- digitization of analog data – especially optical character recognition (OCR)

## Compute: Processors and ML Models
- Developments in procesors and RAM:
- single-core CPU (1970s-1990s)
- multi-core CPU (2000s)
- GPUs (2010s-2020s)
- Developments in ML models:
- classical ML, including regression, decision trees, SVMs (1960s-1980s)
- CPU-based deep learning, including CNNs for computer vision and word embeddings
(2000s-2010s)
- GPU-based deep learning → emergence of transformers (late 2010s)

## Data + ML = Better Digitization
- Early OCR quality was very poor
except with high-quality scans.
- Early solution: better and cheaper
scanners (e.g. Google Books).
- AI-based digitization with pre-trained vision/language models can fill in gaps
based on knowledge and context.
- provides high-quality OCR even with handwriting, or with damaged historical
documents.
- can extract structured data, e.g. tables and charts.
- Meanwhile:
- multilingual AI’s deliver near-flawless machine translation.
- AI-based audio transcription opens up countless hours of voice recordings for
digitization.

## Big data & big compute are preconditions for AI
- With GPT-1 (2018), GPT-2 (2019), and GPT-3 (2020), OpenAI researchers
gradually added more “neurons” so the LLMs can learn more about language.
- Further, the models are pre-trained (that is, they have read) a much larger and
more diverse corpus:
- GPT-2: 8 billion words
- GPT-3: 114 billion words
- 2023-era LLMs (e.g. ChatGPT): 10 trillion words!

## But are big data and compute sufficient for AI?
Data (the whole internet & millions of digitized books)
+
Compute (billions of parameters)
+
ML (transformer architecture)
=
AI systems that are getting close to human-level intelligence
(e.g. GPT-3)

## The Final Ingredient
Data (the whole internet & millions of digitized books)
+
Compute (billions of parameters)
+
ML (transformer architecture)
+
?

## How do children aligned LLMs learn language?
1. They learn to repeat what they hear.
2. They learn some explicit rules from parents/teachers.
3. They get positive and negative feedback when they say something right or wrong.

## From zero-shot learning to human preference alignment

## Outline
From GPT to ChatGPT

## Emergent zero-shot learning (GPT-1, GPT-2)
- As far back as GPT (2018) but especially with GPT-2 (“Language models are
unsupervised multitask learners”, 2019), researchers observed emergent zero-shot
learning.
- e.g.: Passage: [wikipedia excerpt for X]
- Q: Where was X born? →A: ...
- TLDR: ...
Main source for these slides: Jesse Mu’s CS224n slides
https://cs.stanford.edu/~muj/;
https://web.stanford.edu/class/cs224n/slides/cs224n-2023-lecture11-prompting-rlhf.pdf

## Emergent few-shot learning (GPT-3)
“Few-shot learning”: Specify a task and give examples before
the example.
- became especially powerful with GPT-3 (“Language
models are few-shot learners”, 2020)

## Instruction fine-tuning (e.g. FLAN-T5)

## Instruction fine-tuning

## Limitations of instruction fine-tuning
1. Collecting task data is expensive.
2. Some important tasks cannot be written as instructions with correct answers.
e.g., story generation.
3. Language models penalize all mistakes equally at the token level; but some
mistakes are more important than others.
4. Human written answers might be suboptimal.

## Aligning language models with reinforcement learning
- Say we have a language model pθ(y|x) with learnable parameters θ, giving the
probability of output y given input/task x, with samples represented by yˆ(x)
- Further, for each LM task, we have a reward function U(y,x) ∈ R.
- e.g., human scoring/evaluation of LM outputs
- will come back to define U(·), but importantly, does not have to be differentiable.
- We want to choose θ maximize the expected reward from generated samples
yˆθ(x) ∼ pθ(y|x):
θ
∗ = argmax
θ
E{U(ˆyθ(x),x)}
- θ
∗
can be learned via gradient ascent (follow the reward gradient):
θt+1 := θt +α∇θtE{U(ˆyθ(x),x)}
- → solvable by policy gradient methods from reinforcement learning.

## RL in 2 slides (1)
- For a sample of generations S we have
∇θE{U(ˆyθ(x),x)} = ∇θ
X
y∈S
U(ˆyθ(x),x)pθ(y|x) = X
y∈S
U(ˆyθ(x),x)∇θpθ(y|x)
- “log derivative trick”:
∇θ logpθ(y|x) = 1
pθ(y|x)
∇θpθ(y|x) → ∇θpθ(y|x) = pθ(y|x)∇θ logpθ(y|x)
- Then:
X
y∈S
U(ˆyθ(x),x)∇θpθ(y|x) =
X
y∈S
pθ(y|x)U(ˆyθ(x),x)∇θ logpθ(y|x)
= E{U(ˆyθ(x),x)}∇θ logpθ(y|x)
- i.e.,
∇θE{U(ˆyθ(x),x)} = E{U(ˆyθ(x),x)}∇θ logpθ(y|x)

## RL in 2 slides (2)
- From previous slide
∇θE{U(ˆyθ(x),x)} = E{U(ˆyθ(x),x)}∇θ logpθ(y|x)
- LHS cannot be estimated because U(·) is not differentiable.
- RHS can be estimated by Monte Carlo:
≈
1
m
Xm
i=1
U(yi
,x)∇θ logpθ(yi
|x)
where the yi are sampled from the LM.
- i.e., the gradient ascent update is
θt+1 := θt +α
1
m
Xm
i=1
U(yi
,x)∇θ logpθ(yi
|x)
- if U(yi
,x) is positive, change θ to increase pθ(yi
|x)
- if U(yi
,x) is negative, change θ to decrease pθ(yi
|x)
- will converge to optimum θ
∗
(under some assumptions)

## How to model human preferences U(·)
Problem 1: human-in-the-loop is too expensive to measure U(y,x) for all y,x.
- solution: train a text regression model Uϕ(y,x) to predict U from y,x, use that
instead of ground truth.
- E.g., for Stiennon et al (2020), fine-tune a GPT model and add a linear regression
output layer to predict likert scale values (1-7), output Uˆ
ϕ(y,x)
Problem 2: human preference scores are noisy and not well calibrated:
- solution: use pairwise comparisons (revealed preference!) rather than individual
scores.
- Learn scores Uˆ
ϕ(y,x) to predict which of two LM outputs y0,y1 is preferred.
- e.g., with a Bradley-Terry model: minimize the paired comparison loss
L(ϕ) = 1
|D|
X
D
sigmoid(Uϕ(y0,x)−Uϕ(y1,x))
where D is the dataset of pairs (y0,y1), with y0 giving the preferred LM output.
- Uˆ
ϕ(y,x) is a fine-tuned LLM outputting predicted probability that s is preferred in
average pairwise match-up.
- It is the implicit user utility function that ChatGPT is trying to maximize
every time it replies.

## Instruct GPT: Data Collection
- Annotation team:
- 40 annotators from Upwork and ScaleAI.
- Annotators had to pass test annotation task on flagging sensitive content,
completion quality, demo writing, and diversity of group membership.

## Nathan Lambert: “From the Llama 2 numbers and other rumors, it’s likely Meta spent
$10-20 million plus on preference data. This is just for the final model and may not
include extensive experimentation and evaluation.”

## RLHF: Reinforcement Learning with Human Feedback
- Ingredients:
- A pre-trained language model p
PT
θ
(y|x), with parameters θ now frozen
- a reward model Uϕ(y,x)
- RLHF:
- make a copy p
RL
ψ = p
PT
θ
(y|x), with newly trainable parameters ψ
- Use RL with samples S ∼ p
RL
ψ
to learn new parameters ψ, to solve
ψ
∗ = argmax
ψ
X
y∈S
Uϕ(y,x)−β log(
p
RL
ψ
(y|x)
p
PT (y|x)
)
!
- second term penalizes diverging from pre-trained model p
PT (y|x).
- calibrated by hyperparameter β (e.g. β = 0.05 in Stiennen et al 2020).

## Instruct GPT (Ouyang et al 2022)

## Summarization with RLHF (Stiennon et al 2020)

## SFT+RLHF > SFT

## Why RLHF? (Goldberg 2023)
https://gist.github.com/yoavg/6bff0fecd65950898eba1bb321cfbd81
- Why is RLHF needed instead of autoregressive pre-training (APT) or instruction
fine-tuning (IFT)?
- Yoav Goldberg discusses three interesting reasons:
- Flexibility of wording
- Positive and negative feedback
- Preventing hallucination

## Why RLHF 1: Flexibility of Wording
- With autoregressive pre-training, or instruction fine-tuning (without RL), LM has
to replicate the exact wording.
- But with human language, there are many valid ways to convey the same correct
answer.
- APT and IFT "punish" the model for slight deviations from the prescribed text.
The human-provided examples might insist on phrasing which is hard for the
model to learn, while the model already knows how to produce an
alternative---and equally valid---answer.

## Why RLHF 2: Negative Feedback
- SFT models only allow positive feedback; RLHF allows negative feedback.
- in formal learning theory, negative feedback is powerful – without it, an adversarial
teacher can mislead the learner by withhodling some important examples.
- GPT-3 knows what to say; Chat-GPT knows what not to say.
- this makes LM training much closer to how humans learn (language).

## Why RLHF 3: Preventing Hallucination
- LMs have 3 main modes of interaction:
- (1) text-grounded: answer a question or manipulate a provided document
- (2) knowledge-seeking: answer a question from the LM’s knowledge base
- (3) creative: generate something new
- For (2) knowledge-seeking, it is crucial that the model can refuse to answer when
it doesn’t know.
- SFT teaches the model to lie – if it doesn’t know, it will make something up.
- RLHF can prevent this.

## Open Questions on RLHF
- How reliable are (models of) human preferences?
- RLHF can produce chatbots that “seem” right but aren’t.
- How to treat abstaining (“I don’t know” answers)? Should these be penalized
differently?
- Human alignment labeling is cheaper than training GPT-3, but still quite
expensive.
- Fine-tuning new models on ChatGPT outputs will not produce a ChatGPT clone.
- Interesting alternatives: crowd funding, AI feedback

## DPO: Direct Preference Optimization
- LlaMa 3 uses DPO and then PPO.

## ChatGPT = InstructGPT + Conversations

## LlaMa 3 Alignment Pipeline

## GPT-4 = ChatGPT + 10T tokens + 1.7T params (MoE)

## The Final Ingredient
Data (the whole internet & millions of digitized books)
+
Compute (billions of parameters)
+
ML (transformer architecture)
+
Human Preference Alignment (Instruction Tuning and Learning from Feedback)
=
AI systems that often surpass human-level intelligence
(GPT-4+, Gemini, Claude, etc.)

## Prompt: Can you write a proof that there are infinitely many primes, with every line that rhymes?

## “Draw a unicorn in TikZ”

## and many, many, many more examples
- eg coding, vision, interactions, judgment...

## How smart can RLHF LLMs get? Andrej Karpathy
(https://x.com/karpathy/status/1862565643436138619)
- People have too inflated sense of what it means to "ask an AI" about something. The AI
are language models trained basically by imitation on data from human labelers. Instead
of the mysticism of "asking an AI", think of it more as "asking the average data
labeler" on the internet.
- Few caveats apply because e.g. in many domains (e.g. code, math, creative writing) the
companies hire skilled data labelers. . .
- But roughly speaking (and today), you’re not asking some magical AI. You’re asking
a human data labeler. Whose average essence was lossily distilled into statistical token
tumblers that are LLMs. . .
- RLHF moves the performance to "discriminative human" grade, up from SFT
which is at "generative human" grade. But this is not so much "in principle" but
more "in practice", because discrimination is easier for an average person than
generation (e.g. label which of these 5 poems about X is best vs. write a poem about
X). Separately you also get a separate boost from the wisdom of crowds effect, i.e.
your LLM performance is not at human level, but at ensemble of human level.
- So with RLHF in principle the best you can hope for is to reach a performance
where a panel of e.g. the top 10 human experts on some topic, with enough time
given, will pick your answer over any other.

## What should AI Value?
https://docs.google.com/presentation/d/1ekK_U3cAc8e76IzBf7Aq_
nohI4b8F6ECp0nAit_kWd0/edit?usp=sharing