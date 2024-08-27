import datasets
import time
import asyncio
from typing import List, Optional
import os
import openai
import dspy
from dspy import InputField, OutputField, Signature
from dspy.functional import TypedPredictor
import pydantic
from dspy import Example
from dspy.evaluate.evaluate import Evaluate
from dspy.teleprompt.random_search import BootstrapFewShotWithRandomSearch
import re
import traceback
from datetime import datetime
import random
from dspy.teleprompt import MIPROv2
from hackercup_utils import extract_code, run, check_solution
from datetime import datetime

openai.api_key = os.environ.get("OPENAI_API_KEY")
openai.api_base = os.environ.get("OPENAI_API_BASE")

### DEFINING HELPER FUNCTIONS ###

def format_mistakes(solution_results):
    offending_cases = ""
    for offending_case in solution_results["offending_cases"]:
        offending_cases += f"Incorrect Output: {offending_case[1]} -> Expected Output: {offending_case[0]}\n"
    return offending_cases


def get_expected_behavior_str(sample_input, sample_output):
    return f"""
input = {str(sample_input)} # input is of type {type(sample_input)}
output = solve(input)
print(output) # Output is of type string
# Prints: {sample_output}
"""


### DEFINE SIMPLE PIPELINE ###

class GenerateCodeSignature(Signature):
    """You are an expert problem solver. Your task is creating the code to solve the problem at hand in python.

    The program should have a single `solve` method that has the following signature:
    input: [str]: The same Input provided above
    output [str]: The same Output provided above

    Here's an example of the format we'd expect for a simple python program that adds 1 to a number:
    ```python def solve(x: int):\n    return x + 1```

    Note:
    * Do NOT print the Output, instead return it.
    * Make sure that your proposed solution is both time and memory efficient.
    """

    problem_description: str = InputField(format=str)
    expected_behavior: str = InputField(format=str)
    solution: str = OutputField(
        format=str, desc="A plan for how we should go about solving this problem."
    )
    python_program: str = OutputField(format=str)


class SimpleGenerateCode(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate_code = dspy.Predict(GenerateCodeSignature)

    def forward(self, problem_description, sample_input, sample_output):
        expected_behavior = get_expected_behavior_str(sample_input, sample_output)
        python_code = extract_code(
            self.generate_code(
                problem_description=problem_description,
                expected_behavior=expected_behavior,
            ).python_program
        )

        return dspy.Prediction(solution=python_code)


### DEFINE ADVANCED PIPELINE ###

class GenerateCodeString1(Signature):
    """You are an expert problem solver. Your task is creating the code to solve the problem at hand in python.

    The program should have a single `solve` method that has the following signature:
    input: [str]: The same Input provided above
    output [str]: The same Output provided above

    Here's an example of the format we'd expect for a simple python program that adds 1 to a number:
    ```python def solve(x: int):\n    return x + 1```

    Note:
    * Do NOT print( the Output, instead return it.
    * Make sure that your proposed solution is both time and memory efficient.
    """

    # """Given a description of a coding challenge to solve, please respond with the """
    problem_description: str = InputField(format=str)
    expected_behavior: str = InputField(format=str)
    solution: str = OutputField(
        format=str, desc="A plan for how we should go about solving this problem."
    )
    python_program: str = OutputField(format=str)


class GenerateCodeString2(Signature):
    """You are an expert leetcoder who is competing in the HackerCup.

    Your task is to write the code to solve the problem described below.

    The program should have a single `solve` method that has the following signature:
    input: [str]: The same Input provided above
    output [str]: The same Output provided above

    Here's an example of the format we'd expect for a simple python program that adds 1 to a number:
    ```python def solve(x: int):\n    return x + 1```

    Note:
    * Do NOT print( the Output, instead return it.
    * Make sure that your proposed solution is both time and memory efficient.
    """

    # """Given a description of a coding challenge to solve, please respond with the """
    problem_description: str = InputField(format=str)
    expected_behavior: str = InputField(format=str)
    solution: str = OutputField(
        format=str, desc="A plan for how we should go about solving this problem."
    )
    python_program: str = OutputField(format=str)


class GenerateCodeString3(Signature):
    """Given the leetcode problem below, write a solution in python.

    The program should have a single `solve` method that has the following signature:
    input: [str]: The same Input provided above
    output [str]: The same Output provided above

    Here's an example of the format we'd expect for a simple python program that adds 1 to a number:
    ```python def solve(x: int):\n    return x + 1```

    Note:
    * Do NOT print( the Output, instead return it.
    * Make sure that your proposed solution is both time and memory efficient.
    """

    # """Given a description of a coding challenge to solve, please respond with the """
    problem_description: str = InputField(format=str)
    expected_behavior: str = InputField(format=str)
    solution: str = OutputField(
        format=str, desc="A plan for how we should go about solving this problem."
    )
    python_program: str = OutputField(format=str)

class FixCodeWithStackTrace(Signature):
    """We are trying to get this python code to run properly. Please examine the current code and the error, and propose new code."""

    problem_description = InputField()
    current_code = InputField(
        prefix="Current Incorrect Code",
        desc="Current code that is producing the incorrect behavior",
    )
    expected_behavior = InputField()
    stack_trace = InputField()
    explanation = OutputField(desc="Explanation for why the current error is occuring.")
    fixed_code = OutputField()


class FixCodeWithCases(Signature):
    """We are trying to get this python code to run properly. Please examine the current code and the cases that are currently being missed. Propose new code that fixes the error."""

    problem_description = InputField(format=str)
    current_code = InputField(
        prefix="Current Incorrect Code:",
        desc="Current code that is producing the incorrect behavior",
    )
    expected_behavior = InputField(format=str)
    current_incorrect_results = InputField(format=str)
    problems = OutputField(
        prefix="Problem(s):",
        desc="Problem(s) with current code that have led to the incorrect results",
        format=str,
    )
    solutions = OutputField(
        prefix="Solution(s):",
        desc="Solution(s) to problems with existing code",
        format=str,
    )
    fixes = OutputField(
        prefix="Specific Changes to Make to Code:",
        desc="Specific changes we will be making to the code - this can include changes to particular code snippets.",
    )
    fixed_code = OutputField(
        format=str,
        prefix="New Code w/ Fixes Integrated:",
        desc="Code with fixes applied to correct the incorrect behavior. This code MUST be different from the code above, as it should have the fixes integrated. Include a comment next to the changes made to point them out.",
    )


class GenerateCode(dspy.Module):
    def __init__(self, max_tries=3, num_ensembles=5):
        super().__init__()
        # Initialize variables
        self.max_tries = max_tries
        self.num_ensembles = num_ensembles

        # Initialize layers
        # self.generate_code = dspy.Predict(GenerateCodeSignature, n=self.num_ensembles)
        self.generate_code1 = dspy.Predict(GenerateCodeString1)
        self.generate_code2 = dspy.Predict(GenerateCodeString2)
        self.generate_code3 = dspy.Predict(GenerateCodeString3)
        self.fix_code_with_error = dspy.Predict(FixCodeWithStackTrace)
        self.fix_code_with_cases = dspy.Predict(FixCodeWithCases)

    def forward(self, problem_description, sample_input, sample_output):

        expected_behavior = get_expected_behavior_str(sample_input, sample_output)
        python_code1 = extract_code(self.generate_code1(problem_description=problem_description, expected_behavior = expected_behavior).python_program)
        python_code2 = extract_code(self.generate_code2(problem_description=problem_description, expected_behavior = expected_behavior).python_program)
        python_code3 = extract_code(self.generate_code3(problem_description=problem_description, expected_behavior = expected_behavior).python_program)

        python_solutions = [python_code1, python_code2, python_code3]

        for i, python_code in enumerate(python_solutions):
            for try_iter in range(self.max_tries):
                # Test our generated code, get a result
                result_dict = run(code=python_code, input=sample_input, timeout=5)
                error, result, stack_trace = (
                    result_dict["error"],
                    result_dict["result"],
                    result_dict["stack_trace"],
                )
                if error:  # Running code led to an exception, fix code
                    python_code = extract_code(
                        self.fix_code_with_error(
                            problem_description=problem_description,
                            current_code=python_code,
                            expected_behavior=expected_behavior,
                            stack_trace=stack_trace,
                        ).fixed_code
                    )
                elif result is None:  # Nothing was returned by program
                    python_code = extract_code(
                        self.fix_code_with_cases(
                            problem_description=problem_description,
                            current_code=python_code,
                            expected_behavior=expected_behavior,
                            current_incorrect_results="Nothing was returned!",
                        ).fixed_code
                    )
                elif not isinstance(result, str):  # Wrong type returned by program
                    python_code = extract_code(
                        self.fix_code_with_cases(
                            problem_description=problem_description,
                            current_code=python_code,
                            expected_behavior=expected_behavior,
                            current_incorrect_results=f"Returned type {type(result)}, but the result should be a string.",
                        ).fixed_code
                    )
                elif check_solution(sample_output, result)[  # Found a solutiond!
                    "matches"
                ]:
                    print(
                        f"CORRECT SOLN W/ CODE OPTION {i}, DEBUGGING TRY {try_iter+1}."
                    )
                    return dspy.Prediction(solution=python_code)
                else:  # Otherwise, we should be able to check the solution
                    solution_results = check_solution(sample_output, result)
                    # with dspy.context(lm=gpt4):
                    python_code = extract_code(
                        self.fix_code_with_cases(
                            problem_description=problem_description,
                            current_code=python_code,
                            expected_behavior=expected_behavior,
                            current_incorrect_results=format_mistakes(solution_results),
                        ).fixed_code
                    )
        return dspy.Prediction(solution=python_code)

### OPTIMIZATION ### 

def optimize_with_mipro(program, prompt_model, task_model, metric, trainset):
    teleprompter = MIPROv2(
        prompt_model=prompt_model,
        task_model=task_model,
        metric=metric,
        num_candidates=5,
        init_temperature=0.5,
        verbose=False,
        log_dir="/lfs/0/kristaoo/dspy/examples/functional/logs",
    )

    optimized_program = teleprompter.compile(
        program.deepcopy(),
        trainset=trainset,
        eval_kwargs=dict(num_threads=16),
        max_bootstrapped_demos=0, # 0-shot optimization
        max_labeled_demos=0,
        num_batches=20,
        minibatch=False, # turning this off bc we have a small trainset already
        seed=9
    )

    now = datetime.now()
    date_time = now.strftime("%Y%m%d_%H%M%S")

    optimized_program.save(f"mipro_optimized_{date_time}")

    return optimized_program

def optimize_with_bootstrap_fewshot(program, task_model, teacher_model, metric, trainset):
    rs_optimizer = BootstrapFewShotWithRandomSearch(
        metric=test_code(timeout=5),
        num_threads=8,
        num_candidate_programs=5,
        max_labeled_demos=0,
        max_bootstrapped_demos=2,
        max_errors =10000,
        teacher_settings=dict(lm=teacher_model)
    )
    
    optimized_program = rs_optimizer.compile(
        program,
        trainset=trainset,
    )

    now = datetime.now()
    date_time = now.strftime("%Y%m%d_%H%M%S")

    optimized_program.save(f"fewshot_optimized_{date_time}")


    return optimized_program

### DEFINING FUNCTION FOR TESTING CODE TO USE AS METRIC ###
### TODO: why this syntax?
def test_code(timeout=5):
    def metric(example, pred, trace=None):
        if pred.solution is None:
            return 0
        solution_code = pred.solution
        result_dict = run(
            code=solution_code, input=example.sample_input, timeout=timeout
        )
        if not result_dict["result"]:
            return 0
        return int(
            check_solution(example.sample_output, result_dict["result"])["matches"]
        )

    return metric


if __name__ == "__main__":

    ### LOAD AND PREPARE DATA ### 
    ds = datasets.load_dataset("hackercupai/hackercup")

    # Shuffle data 
    ds_full_list = list(ds["full"])
    rng = random.Random(0)
    rng.shuffle(ds_full_list)

    # Format dataset to use in DSPy
    # TODO: what does this syntax mean 
    sample_ds = [
        Example(
            problem_description=example["statement"],
            sample_input=example["sample_input"].strip().split("\n"),
            sample_output=example["sample_output"],
        ).with_inputs("problem_description", "sample_input", "sample_output")
        for example in ds["sample"]
        if example["sample_input"]
    ]

    full_ds = [
        Example(
            problem_description=example["statement"],
            sample_input=example["sample_input"].strip().split("\n"),
            sample_output=example["sample_output"],
        ).with_inputs("problem_description", "sample_input", "sample_output")
        for example in ds_full_list
        if example["sample_input"]
    ]

    trainset = sample_ds + full_ds[0:40] # use sample in train because it's easier 
    testset = full_ds[40:90]

    # Configure our dspy settings (particularly LM we're using)
    lm = dspy.OpenAI(
        model="gpt-4o-mini-2024-07-18", # Note: didn't find much a difference btwn mini & full gpt-4o
        max_tokens=4000,
        temperature=0.1,
    )

    dspy.settings.configure(lm=lm)
    dspy.configure(experimental=True)

    # Setup evaluation function
    evaluate = Evaluate(
        devset=testset,
        num_threads=1, # Note: Set this to 1 for debugging purposes 
        display_progress=True,
        display_table=5,
        metric=test_code(timeout=5)
    )

    # Try out a simple program (7.5% on 40 ex)
    # simple_program = SimpleGenerateCode()
    # print(f"Evaluating Simple Program on test...")
    # evaluate(program=simple_program, devset=testset)

    # Try out more advanced pipeline (22.5% on 40 ex)
    multi_stage_program = GenerateCode()
    print(f"Evaluating Multi-Stage Program on test...")
    evaluate(program=multi_stage_program, devset=testset)

    # OPTIONAL: Optimize program w/ MIPROv2 (0-shot)
    # multi_stage_program = GenerateCode()
    # mipro_optimized_multi_stage_program = optimize_with_mipro(multi_stage_program, lm, lm, test_code(timeout=5), trainset)
    # print(f"Evaluating MIPRO optimized Multi-Stage Program on test...")
    # evaluate(program=mipro_optimized_multi_stage_program, devset=testset)

    # OPTIONAL: Optimize program w/ MIPROv2 (0-shot)
    # multi_stage_program = GenerateCode()
    # bootstrap_optimized_multi_stage_program = optimize_with_bootstrap_fewshot(multi_stage_program, lm, lm, test_code(timeout=5), trainset)
    # print(f"Evaluating Bootstrap Few-Shot optimized Multi-Stage Program on test...")
    # evaluate(program=bootstrap_optimized_multi_stage_program, devset=testset)
