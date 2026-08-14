#!/usr/bin/env python3
"""Seed a demo corpus so the app and the eval run without Canvas or API keys.

Creates two courses, five assignments, twelve notes (including deliberate
distractors), and a labelled eval set. The distractors matter: an eval set made
only of obvious positives cannot distinguish a good retriever from one that
returns everything.

    python scripts/seed_demo.py [--keep]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from studylink import store  # noqa: E402
from studylink.config import load_settings  # noqa: E402
from studylink.db import reset  # noqa: E402
from studylink.evaluation.dataset import LabeledPair, save_labels, sync_to_db  # noqa: E402
from studylink.service import StudyLink  # noqa: E402

COURSES = [
    ("101", "CS 4780: Machine Learning", "CS4780"),
    ("102", "HIST 2100: Modern European History", "HIST2100"),
]

ASSIGNMENTS = [
    (
        "101",
        "2001",
        "Problem Set 3: Gradient Descent and Regularization",
        "Implement batch and stochastic gradient descent for linear regression. Compare "
        "convergence under different learning rates. Then add L1 and L2 regularization to "
        "your model and explain, with reference to the loss surface, why L1 produces sparse "
        "weights while L2 shrinks them smoothly toward zero. Report the effect of the "
        "regularization strength lambda on training and validation error.",
        "2026-09-20T23:59:00Z",
        100.0,
        "online_upload",
    ),
    (
        "101",
        "2002",
        "Project Milestone 1: Dataset and Baseline Model",
        "Choose a dataset for your final project and justify the choice. Describe how you "
        "split the data into training, validation, and test sets, and why that split is "
        "appropriate for your problem. Train one simple baseline model and report its "
        "performance. A baseline that is too strong or too weak makes the rest of the "
        "project uninterpretable, so explain what your baseline establishes.",
        "2026-10-04T23:59:00Z",
        75.0,
        "online_text_entry",
    ),
    (
        "101",
        "2003",
        "Quiz 2 Review: Decision Trees and Ensembles",
        "Covers decision tree construction, information gain and Gini impurity, pruning, and "
        "ensemble methods. You should be able to explain how bagging reduces variance, how "
        "boosting reduces bias, and when a random forest is preferable to a single deep tree.",
        "2026-10-15T18:00:00Z",
        50.0,
        "online_quiz",
    ),
    (
        "102",
        "2004",
        "Essay 2: Causes of the First World War",
        "Write a 2000-word essay arguing for the most important cause of the outbreak of war "
        "in 1914. You must engage with the alliance system and the diplomatic crisis of July "
        "1914, and you must take a position in the historiographical debate over German "
        "responsibility. Essays that summarise events without making an argument will not "
        "receive full marks.",
        "2026-10-10T23:59:00Z",
        200.0,
        "online_upload",
    ),
    (
        "102",
        "2005",
        "Primary Source Analysis: Interwar Propaganda",
        "Select one piece of visual propaganda produced in Europe between 1919 and 1939. "
        "Analyse its visual rhetoric, its intended audience, and the political conditions it "
        "was responding to. Your analysis should treat the source as evidence about its "
        "makers, not simply as an illustration of events.",
        "2026-11-01T23:59:00Z",
        120.0,
        "online_text_entry",
    ),
]

NOTES = [
    (
        "101",
        "Lecture 5 - Gradient Descent",
        "note",
        """Gradient descent minimises a loss function by repeatedly stepping in the direction of
steepest descent. The update rule is w := w - alpha * gradient of the loss with respect to w.
Alpha is the learning rate and it is the parameter that decides whether this works at all.

Learning rate behaviour, from lecture:
- Too small: convergence is correct but extremely slow, and you may hit the iteration cap
  before reaching the minimum.
- Too large: the update overshoots the minimum and the loss oscillates or diverges outright.
- A good practical starting point is 0.01 and then tune by watching the training curve.

Batch gradient descent computes the gradient over the entire training set before each update.
It is stable and every step genuinely decreases the loss, but each step costs a full pass over
the data.

Stochastic gradient descent (SGD) updates using one example at a time. Each individual step is
noisy and can increase the loss, but it makes far more updates per pass over the data, so in
practice it converges much faster on large datasets. The noise also helps escape shallow local
minima. Mini-batch gradient descent (typically 32-256 examples) is the compromise used in
practice: it gets most of the speed of SGD with much less gradient variance.

Convergence check: plot training loss against iteration. A smooth decreasing curve means the
learning rate is reasonable. A jagged or increasing curve means it is too high.""",
    ),
    (
        "101",
        "Lecture 6 - Regularization: L1 vs L2",
        "note",
        """Regularization adds a penalty on the size of the weights to the loss function, to stop
the model fitting noise in the training set. The strength of the penalty is lambda.

L2 regularization (ridge) adds lambda times the sum of squared weights. The gradient of the
penalty is proportional to w itself, so every weight is pulled toward zero by an amount
proportional to its current size. Large weights shrink fast, small weights shrink slowly, and
nothing quite reaches zero. Geometrically the constraint region is a circle, and the loss
contour usually touches it away from the axes.

L1 regularization (lasso) adds lambda times the sum of absolute weights. The gradient of the
penalty is a constant sign, so every weight is pushed toward zero by the same fixed amount
regardless of size. Small weights hit exactly zero and stay there, which is why L1 produces
sparse models and acts as feature selection. Geometrically the constraint region is a diamond
with corners on the axes, and the loss contour tends to touch a corner, where some coordinates
are exactly zero.

Effect of lambda: as lambda increases, training error rises monotonically while validation
error falls and then rises again. The minimum of the validation curve is the value to pick.
That U-shape is the bias-variance tradeoff made visible.""",
    ),
    (
        "101",
        "Section notes - Train/validation/test splits and baselines",
        "note",
        """Why three splits and not two. The training set fits the parameters. The validation set
chooses hyperparameters and decides when to stop. The test set is touched once, at the end. If
you tune against the test set you no longer have an unbiased estimate of generalisation - you
have just fitted the test set slowly, by hand.

Typical split is 60/20/20, or 80/10/10 when data is plentiful. Two cases where a random split
is wrong:
- Time series: split by time, never randomly. A random split lets the model see the future.
- Grouped data (multiple samples per patient, per user, per document): split by group, or the
  same entity appears in both training and test and the score is inflated.

Baselines. Always establish a baseline before building anything complicated. For classification
the standard baselines are majority class and a logistic regression on simple features. For
regression, predicting the mean. The baseline is what tells you whether your real model earned
its complexity. A project that reports 85% accuracy means nothing until you know the majority
class is 84%.""",
    ),
    (
        "101",
        "Office hours - project dataset selection",
        "note",
        """Notes from office hours on picking a final project dataset.

What the instructor is looking for in the justification: the dataset has to be big enough that
a learned model can beat a trivial baseline, and the prediction target has to be something a
model could plausibly know from the features. Predicting a target that is not determined by the
inputs produces a project that reports failure for the whole semester.

Things to check before committing to a dataset:
- Size: a few thousand rows minimum for anything beyond linear models.
- Label quality: who labelled it, and how noisy is it? Noisy labels put a ceiling on accuracy.
- Class balance: heavily imbalanced targets need stratified splits and a metric that is not
  plain accuracy.
- Leakage: is there a feature that encodes the answer? A column recorded after the outcome is
  known will make the baseline suspiciously strong.

Advice on baselines specifically: report the baseline in the milestone even if it is
embarrassing. The milestone is graded on whether the setup is sound, not on the score.""",
    ),
    (
        "101",
        "Lecture 9 - Decision Trees",
        "note",
        """A decision tree splits the feature space by asking one feature-threshold question at a
time, recursively, until the leaves are pure enough to predict.

Choosing the split. At each node we pick the split that most reduces impurity.
- Entropy is -sum p_i log p_i. Information gain is the entropy of the parent minus the weighted
  entropy of the children.
- Gini impurity is 1 - sum p_i squared. Cheaper to compute and in practice picks nearly the same
  splits as entropy.

Overfitting. A tree grown until every leaf is pure will memorise the training set: it can always
isolate a single noisy example behind enough splits. Training error goes to zero and validation
error climbs. Controls are maximum depth, minimum samples per leaf, and minimum impurity
decrease.

Pruning. Grow the tree fully, then remove subtrees whose removal does not hurt validation
performance (cost-complexity pruning). Post-pruning generally beats aggressive early stopping,
because a split that looks useless can enable a useful split beneath it.

Trees are readable, handle mixed feature types, and need no feature scaling. Their weakness is
variance: change a few training points and the top split can change, which changes everything
below it.""",
    ),
    (
        "101",
        "Lecture 10 - Random Forests and Boosting",
        "transcript",
        """[Lecture transcript, week 10]

So last week we established that a single deep decision tree has low bias and very high
variance. Today is about the two ways of fixing that, and they fix different halves of the
error.

Bagging first. You take bootstrap samples of the training set, fit one tree per sample, and
average the predictions. Averaging many high-variance, roughly unbiased estimators reduces
variance without adding bias. The catch is that trees trained on bootstrap samples of the same
data are highly correlated - they all pick the same strong feature at the root - and correlated
errors do not average away.

Random forests fix that. At each split you consider only a random subset of features, typically
the square root of the total for classification. Now the trees are forced to disagree, they
decorrelate, and the variance reduction from averaging actually materialises. That is the whole
idea: bagging plus feature subsampling.

Boosting attacks the other term. You fit a sequence of weak learners - shallow trees, often
depth one or two - where each one is fitted to the residual errors of the ensemble so far. Each
learner is high bias on its own, and the sequence drives the bias down. Because you are fitting
errors, boosting can overfit if you run too many rounds, so the number of rounds is a
hyperparameter you tune on validation, and you shrink each contribution by a learning rate.

The practical summary: random forests are hard to misuse and give you a strong result with
almost no tuning. Gradient boosting will usually beat them if you tune it, and will do worse if
you do not.""",
    ),
    (
        "101",
        "Lecture 2 - Linear algebra refresher",
        "note",
        """Refresher before the models start.

Vectors and dot products. The dot product of two vectors is the sum of elementwise products.
Geometrically it is the length of one vector times the projection of the other onto it. A dot
product of zero means orthogonal.

Matrix multiplication. (AB)_ij is the dot product of row i of A with column j of B. Not
commutative. Dimensions must line up: (n x k)(k x m) gives (n x m). Most shape errors in code
come from forgetting this.

Norms. The L2 norm is the square root of the sum of squares - ordinary Euclidean length. The L1
norm is the sum of absolute values. We will see both again.

Eigenvectors and eigenvalues. Av = lambda v means v is a direction that the matrix only
stretches, never rotates. These come back in principal component analysis, where the
eigenvectors of the covariance matrix are the directions of greatest variance.

Matrix calculus, the two identities we will actually use: the gradient of w-transpose-x with
respect to w is x, and the gradient of w-transpose-A-w is 2Aw for symmetric A.""",
    ),
    (
        "102",
        "Lecture 3 - The alliance system before 1914",
        "note",
        """By 1907 Europe had sorted itself into two armed blocs, and that structure is what turned
a Balkan quarrel into a continental war.

The Triple Alliance: Germany, Austria-Hungary, Italy (1882). Bismarck's design, intended to
keep France isolated. Italy's commitment was always conditional and Italy did not in fact fight
alongside its allies in 1914.

The Triple Entente: France and Russia (1894), Britain and France (Entente Cordiale, 1904),
Britain and Russia (1907). Note that the Entente was a set of understandings rather than a
formal mutual-defence treaty - Britain's obligation in 1914 was ambiguous, which mattered enormously
in the last week of peace.

Why the structure was dangerous. Any two-power dispute could pull in four more. Mobilisation
schedules were built on the assumption of a two-front war, so military planning in one capital
forced the timetable in another. The Schlieffen Plan in particular meant that a German war with
Russia was automatically also a war with France, and via Belgium, very probably with Britain.

Counter-argument to keep in mind for the essay: alliances had also defused earlier crises -
Morocco in 1905 and 1911, Bosnia in 1908 - without war. The system alone does not explain 1914;
you need the decisions taken in July.""",
    ),
    (
        "102",
        "Lecture 4 - The July Crisis, 1914",
        "note",
        """Timeline of the five weeks between the assassination and general war.

28 June: Franz Ferdinand assassinated in Sarajevo by Gavrilo Princip, of the Black Hand.
5-6 July: The "blank cheque" - Germany assures Austria-Hungary of unconditional support, which
removes Vienna's main restraint.
23 July: Austrian ultimatum to Serbia, deliberately drafted to be unacceptable.
25 July: Serbia accepts nearly every term. Austria declares the reply insufficient.
28 July: Austria-Hungary declares war on Serbia.
30 July: Russia orders general mobilisation in support of Serbia.
1 August: Germany declares war on Russia. 3 August: on France.
4 August: German troops enter Belgium; Britain declares war.

The mechanism worth arguing about in the essay: mobilisation timetables compressed the time
available for diplomacy. Russian general mobilisation could not be limited to the Austrian
frontier, and German war planning treated Russian mobilisation as the trigger for immediate
action in the west. Statesmen were making decisions on railway schedules rather than political
judgement by the end of July.

Historiographical note: Fritz Fischer's argument that Germany deliberately sought a European war
turns largely on how you read the blank cheque and the pressure Berlin put on Vienna between 5
and 23 July.""",
    ),
    (
        "102",
        "Reading notes - the Fischer thesis debate",
        "note",
        """Fritz Fischer, Germany's Aims in the First World War (1961), and the controversy that
followed.

Fischer's claim: Germany did not stumble into war in 1914 but pursued it, as a bid for
continental hegemony consistent with war aims documented after the fighting started. His
evidence includes the September Programme of 1914 and the encouragement given to Vienna in
early July. The implication - continuity between Wilhelmine and Nazi expansionism - is what made
the argument explosive in West Germany.

Criticisms. Gerhard Ritter argued Fischer read post-hoc war aims backwards into pre-war
intentions. Others noted that every great power had expansionist factions and that singling out
Germany reflects hindsight about 1939 rather than evidence about 1914.

Later positions worth citing. James Joll emphasised the "unspoken assumptions" - the shared
militarist and social-Darwinist mental world - rather than a single guilty state. Christopher
Clark's The Sleepwalkers (2012) redistributes responsibility across all the powers and
emphasises Serbian and Russian decisions that Fischer's frame downplays.

For an essay taking a position: the strongest version of a Fischer-influenced argument is not
"Germany planned the war" but "Germany was willing to risk a general war in July 1914 in a way
the other powers were not, and that asymmetry of risk-tolerance is decisive".""",
    ),
    (
        "102",
        "Lecture 8 - Visual propaganda between the wars",
        "note",
        """How to read an interwar propaganda poster as evidence.

The core move: a poster is evidence about its makers and its intended audience, not evidence
that its claims were true. Ask what anxiety the image is addressing. A poster insisting on
national unity is evidence that unity was in doubt.

Visual rhetoric to look for:
- Scale and viewpoint. Low angles monumentalise the subject; crowds rendered as uniform mass
  imply a people with one will.
- Colour. Soviet and later fascist poster design both lean on red and black with heavy
  diagonals for urgency.
- Typography as image. Weimar-era political posters often use hand-drawn Fraktur to signal
  tradition, or hard sans-serif to signal modernity and machine-age efficiency.
- The enemy figure: caricatured, usually smaller, often placed at the lower edge of the frame.

Context that changes the reading: the 1923 hyperinflation and the 1929 crash both produce sharp
shifts in poster content toward economic grievance. Dating the source within a couple of years
matters more than in most primary-source work, because the political conditions moved so fast.

Audience question to answer explicitly: was it produced for the party faithful, for the
undecided, or for an external audience? The rhetoric differs and the intended audience is
usually recoverable from where the poster was posted and what it assumes the viewer already
believes.""",
    ),
    (
        "102",
        "Lecture 14 - Postwar reconstruction after 1945",
        "note",
        """Europe after the Second World War: the reconstruction settlement.

The Marshall Plan (1948-51) transferred roughly 13 billion dollars to western Europe. The
economic effect is debated - some argue recovery was already under way - but the political
effect is not: it tied recipient economies to the United States and accelerated the division of
the continent.

Bretton Woods and the institutions. Fixed exchange rates pegged to the dollar, the IMF, and the
World Bank, designed explicitly to avoid the competitive devaluations and trade collapse of the
1930s. The interwar failure was the reference point for every choice made in 1944.

The German question. Occupation zones hardened into two states by 1949. Denazification was
inconsistent and largely abandoned in the west as Cold War priorities took over.

Early European integration: the Coal and Steel Community (1951) placed the industries of war
under joint authority, less for economic efficiency than to make another Franco-German war
materially difficult.""",
    ),
]

# Hand labels. Positives are notes that should surface; negatives are plausible
# distractors -- same course, overlapping vocabulary -- that should not.
LABELS = [
    # PS3: gradient descent and regularization
    LabeledPair("Problem Set 3: Gradient Descent and Regularization", "Lecture 5 - Gradient Descent", True,
                "Directly covers the learning-rate comparison the problem set asks for."),
    LabeledPair("Problem Set 3: Gradient Descent and Regularization", "Lecture 6 - Regularization: L1 vs L2", True,
                "Covers the L1 sparsity vs L2 shrinkage explanation and the lambda sweep."),
    LabeledPair("Problem Set 3: Gradient Descent and Regularization", "Lecture 2 - Linear algebra refresher", False,
                "Mentions L1 and L2 norms but gives no help with the actual task."),
    LabeledPair("Problem Set 3: Gradient Descent and Regularization", "Lecture 9 - Decision Trees", False,
                "Same course, unrelated model family."),
    # Project milestone 1
    LabeledPair("Project Milestone 1: Dataset and Baseline Model",
                "Section notes - Train/validation/test splits and baselines", True,
                "Covers both required elements: the split rationale and what a baseline establishes."),
    LabeledPair("Project Milestone 1: Dataset and Baseline Model", "Office hours - project dataset selection", True,
                "Directly about justifying the dataset choice for this milestone."),
    LabeledPair("Project Milestone 1: Dataset and Baseline Model", "Lecture 5 - Gradient Descent", False,
                "Optimisation detail, not relevant to dataset selection or splits."),
    # Quiz 2
    LabeledPair("Quiz 2 Review: Decision Trees and Ensembles", "Lecture 9 - Decision Trees", True,
                "Information gain, Gini, and pruning are all named in the quiz scope."),
    LabeledPair("Quiz 2 Review: Decision Trees and Ensembles", "Lecture 10 - Random Forests and Boosting", True,
                "Covers the bagging-variance and boosting-bias contrast the quiz asks for."),
    LabeledPair("Quiz 2 Review: Decision Trees and Ensembles",
                "Section notes - Train/validation/test splits and baselines", False,
                "Methodology note, not on the quiz topic list."),
    LabeledPair("Quiz 2 Review: Decision Trees and Ensembles", "Lecture 6 - Regularization: L1 vs L2", False,
                "Overfitting is discussed in both, but the quiz is about trees."),
    # Essay 2
    LabeledPair("Essay 2: Causes of the First World War", "Lecture 3 - The alliance system before 1914", True,
                "The essay explicitly requires engaging with the alliance system."),
    LabeledPair("Essay 2: Causes of the First World War", "Lecture 4 - The July Crisis, 1914", True,
                "The essay explicitly requires the July 1914 diplomatic crisis."),
    LabeledPair("Essay 2: Causes of the First World War", "Reading notes - the Fischer thesis debate", True,
                "Supplies the historiographical position the essay demands."),
    LabeledPair("Essay 2: Causes of the First World War", "Lecture 14 - Postwar reconstruction after 1945", False,
                "Wrong war, wrong decade."),
    LabeledPair("Essay 2: Causes of the First World War", "Lecture 8 - Visual propaganda between the wars", False,
                "Same course and adjacent period, but no bearing on 1914 causation."),
    # Primary source analysis
    LabeledPair("Primary Source Analysis: Interwar Propaganda",
                "Lecture 8 - Visual propaganda between the wars", True,
                "Gives the exact analytical framework the assignment asks for."),
    LabeledPair("Primary Source Analysis: Interwar Propaganda",
                "Lecture 14 - Postwar reconstruction after 1945", False,
                "Post-1945, outside the 1919-1939 window."),
    LabeledPair("Primary Source Analysis: Interwar Propaganda", "Lecture 4 - The July Crisis, 1914", False,
                "Pre-dates the interwar period the source must come from."),
    LabeledPair("Primary Source Analysis: Interwar Propaganda", "Lecture 10 - Random Forests and Boosting", False,
                "Cross-course distractor."),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Do not wipe existing data first")
    args = parser.parse_args()

    settings = load_settings()
    app = StudyLink(settings)

    if not args.keep:
        reset(app.conn)

    course_ids: dict[str, int] = {}
    for canvas_id, name, code in COURSES:
        course_ids[canvas_id] = store.upsert_course(app.conn, canvas_id, name, code)

    for course_key, canvas_id, name, description, due, points, submission in ASSIGNMENTS:
        store.upsert_assignment(
            app.conn,
            course_id=course_ids[course_key],
            canvas_id=canvas_id,
            name=name,
            description=description,
            due_at=due,
            points_possible=points,
            submission_types=submission,
            html_url=f"https://canvas.example.edu/courses/{course_key}/assignments/{canvas_id}",
        )

    for course_key, title, source_type, body in NOTES:
        store.create_note(
            app.conn,
            title=title,
            body=body.strip(),
            course_id=course_ids[course_key],
            source_type=source_type,
        )

    save_labels(LABELS, settings.labels_path)
    written = sync_to_db(app.conn, LABELS)

    stats = app.reindex(force=True)
    status = app.status()

    print("Seeded demo corpus.")
    print(f"  courses:     {status.courses}")
    print(f"  assignments: {status.assignments}")
    print(f"  notes:       {status.notes}")
    print(f"  index:       {stats.summary}")
    print(f"  provider:    {status.provider}")
    print(f"  labels:      {written} pair(s) -> {settings.labels_path}")
    print("\nNext: streamlit run app.py    (or: python scripts/run_eval.py --sweep)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
