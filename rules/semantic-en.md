# Semantic signs of AI slop in English

Revizor's base catalog for the semantic pass on English documents. The agent reads it when the owner has no style skill for English. Regexes in `en.json` already catch the mechanics (not just X but Y, fillers, model vocabulary, -ing riders, staccato), so this file lists only what a regex cannot catch.

Signs are sorted by strength. Strong ones are fixed on a single hit, weak ones only when other signs sit nearby.

## Strong

**moral – a moral under the block.** A paragraph or slide closes with a one-line conclusion that repeats what was said above: "Clarity beats cleverness.", "And that changes everything." Cut it unless the line adds a fact, a number or a practical step.

**antithesis – a contrast spread over two sentences.** "This isn't about price. It's about trust." Keep the claim. Drop the negation, or state it separately if it is really needed.

**mirror – chiasmus and mirrored pairs.** "We don't chase customers, customers find us." A reversed construction sounds like a slogan. Say what actually happens.

**triplet – a slogan triad.** Three short words or phrases in a row, in a heading or a closing line: "Fast. Simple. Reliable.", "speed, quality and transparency". Keep what a fact supports, cut the rest.

**announce – announcing the next block.** "Here's the thing.", "Before we look at the numbers, it's worth asking why they matter." Start with the content.

**strawman – arguing with no one.** "It's easy to assume X. But…" Nobody claimed X. Drop the set-up and keep the claim.

**thrice – one idea three times.** A claim, its paraphrase and a conclusion from it in neighbouring sentences. Keep one wording, the most concrete.

**evaluation – a verdict instead of a fact.** "Significant growth", "impressive results", "a key role" with no number or comparison. Intensifiers such as "truly", "incredibly", "absolutely" and the words "innovative", "powerful", "robust" without evidence belong here too. Put in the number or fact from the text, otherwise drop the verdict.

**summary-end – a recap paragraph at the end.** The last paragraph retells what came before: "In summary, …", "Overall, we can see that …". Delete it. End on the last new point or on an action.

**rhetorical-qa – a question and its answer.** "Why does this matter? Because …", "The result? Fewer bugs." Keep the answer as a statement.

## Medium

**hidden-actor – a verb that hides who acted.** "The files go away", "the issue resolved itself", "costs were optimized". Name who did what, with a number if there is one.

**label – a label fragment.** A verbless tag after an explanation: "Two independent levers.", "A classic trap." Fold it into a sentence with a verb or cut it.

**repeat-detail – a detail that travels between blocks.** The same line, number or caveat appears in neighbouring sections although each section answers a different question. Keep it where it answers that section's question.

**metaphor-cliche – a stock metaphor.** Journey, landscape, ecosystem, tapestry, symphony, bridge between, north star, unlock. Name the thing directly.

**hedge – stacked qualifiers.** "Could potentially help", "may arguably improve", "is designed to enable". Say what it does, or what it does not do.

**precision-without-condition – an exact number without its condition.** "Conversion grew 23%" with no segment, period or baseline. Bring back the condition from the source, or flag the number to the owner.

## Weak

Fixed only together with other signs nearby.

**pairs – paired adjectives.** "Simple and intuitive", "fast and reliable". Keep one, the one that is supported.

**passive – passive voice and nominalisations.** "A decision was made", "implementation of the integration was performed". Bring back the actor and the verb.

**hyphen-pairs – hyphenated compounds everywhere.** "data-driven, user-centric, future-proof" in one paragraph. Keep the one that carries meaning.

**even-rhythm – even rhythm.** Paragraphs of the same length and skeleton, every sentence 15-20 words, sentences chained with "and". Mark the place where the rhythm shows most and split or merge sentences there.
