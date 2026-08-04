class IngestionJobRepositoryError(RuntimeError):
    pass


class IngestionJobLeaseLostError(IngestionJobRepositoryError):
    pass
