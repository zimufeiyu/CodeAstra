from pydantic import BaseModel, Field
class LoginRequest(BaseModel): username: str = Field(min_length=1, max_length=100); password: str = Field(repr=False)
class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=8, max_length=1024, repr=False)
class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)

class BatchStatusRequest(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=500)
    active: bool
