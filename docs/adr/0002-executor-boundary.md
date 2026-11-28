# Executor boundary

## Proposed flow

```text
Worker:
    check the budget
    prepare the prompt and pick the exact Git commit to start from
    send the task to the Executor and wait for the result
    save the result as a Report

Executor:
    create a worktree starting at that Git commit
    run the agent in the worktree
    run the tests there
    commit the changes
    return the test result, any errors, and the branch name
```

## Retries

- **Same attempt, same inputs.** Return the existing run. This applies while it is running and after it finishes.
- **Same attempt, different inputs.** Reject the request so two runs cannot use the same attempt number for the same task.
- **New attempt.** Keep the branch reserved until the Manager finishes reviewing the previous attempt. If the Manager decides to retry, the next attempt can then reuse the branch. This prevents a retry from replacing changes the Manager is about to merge.
