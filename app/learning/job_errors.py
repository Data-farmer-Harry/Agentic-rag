class LearningJobRepositoryError(RuntimeError):
    pass


class LearningJobLeaseLostError(LearningJobRepositoryError):
    pass
