# app/domain/exceptions.py

class DomainException(Exception):
    """Base class for domain exceptions."""
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code
        self.detail = message

class DocumentNotFoundException(DomainException):
    def __init__(self, document_id: str = None, message: str = "Document not found."):
        super().__init__(message, status_code=404)
        self.document_id = document_id

class DuplicateDocumentException(DomainException):
    def __init__(self, filename: str = None, message: str = "Duplicate document found."):
        super().__init__(message, status_code=409)
        self.filename = filename

class OperationConflictException(DomainException):
    def __init__(self, message: str = "Operation cannot be completed due to a conflict."):
        super().__init__(message, status_code=409)

class InvalidInputException(DomainException):
    def __init__(self, message: str = "Invalid input provided."):
        super().__init__(message, status_code=400)

class ServiceDependencyException(DomainException):
    def __init__(self, service_name: str, original_error: str = "", message: str = "Error with a dependent service."):
        detailed_message = f"{message} Service: {service_name}. Original error: {original_error}"
        super().__init__(detailed_message, status_code=503)
        self.service_name = service_name
        self.original_error = original_error

class StorageException(DomainException):
    def __init__(self, message: str = "Storage operation failed."):
        super().__init__(message, status_code=503)

class DatabaseException(DomainException):
    def __init__(self, message: str = "Database operation failed."):
        super().__init__(message, status_code=503)

class VectorStoreException(DomainException):
    def __init__(self, message: str = "Vector store operation failed."):
        super().__init__(message, status_code=503)