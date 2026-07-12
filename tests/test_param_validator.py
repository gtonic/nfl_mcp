"""Tests for param_validator module (validate_params, format_errors)."""
import pytest
from nfl_mcp.param_validator import validate_params, format_errors


class TestValidateParams:
    """Test validate_params function."""

    def test_validate_params_basic(self):
        """Test basic parameter validation."""
        schema = {
            "name": {"type": str, "required": True},
            "age": {"type": int, "required": True}
        }
        values = {"name": "John", "age": 30}
        
        validated, errors = validate_params(schema, values)
        
        assert len(errors) == 0
        assert validated["name"] == "John"
        assert validated["age"] == 30

    def test_validate_params_missing_required(self):
        """Test validation fails when required parameter is missing."""
        schema = {
            "name": {"type": str, "required": True},
            "age": {"type": int, "required": True}
        }
        values = {"name": "John"}  # age is missing
        
        validated, errors = validate_params(schema, values)
        
        assert len(errors) > 0
        assert "age" in errors[0].lower() or "required" in errors[0].lower()

    def test_validate_params_default_value(self):
        """Test default value is applied."""
        schema = {
            "name": {"type": str, "required": True},
            "debug": {"type": bool, "default": False}
        }
        values = {"name": "John"}
        
        validated, errors = validate_params(schema, values)
        
        assert len(errors) == 0
        assert validated["debug"] is False

    def test_validate_params_type_checking(self):
        """Test type checking for parameters."""
        schema = {
            "count": {"type": int, "required": True}
        }
        values = {"count": "not_an_int"}  # String instead of int
        
        validated, errors = validate_params(schema, values)
        
        assert len(errors) > 0
        assert "type" in errors[0].lower() or "int" in errors[0].lower()

    def test_validate_params_numeric_bounds(self):
        """Test numeric bounds validation."""
        schema = {
            "age": {"type": int, "min": 0, "max": 120}
        }
        
        # Too low
        values = {"age": -1}
        validated, errors = validate_params(schema, values)
        assert len(errors) > 0
        assert ">=" in errors[0]
        
        # Too high
        values = {"age": 150}
        validated, errors = validate_params(schema, values)
        assert len(errors) > 0
        assert "<=" in errors[0]
        
        # Valid
        values = {"age": 30}
        validated, errors = validate_params(schema, values)
        assert len(errors) == 0

    def test_validate_params_choices(self):
        """Test choices validation."""
        schema = {
            "status": {"type": str, "choices": ["active", "inactive", "pending"]}
        }
        
        # Valid choice
        values = {"status": "active"}
        validated, errors = validate_params(schema, values)
        assert len(errors) == 0
        
        # Invalid choice
        values = {"status": "unknown"}
        validated, errors = validate_params(schema, values)
        assert len(errors) > 0
        assert "one of" in errors[0].lower()

    def test_validate_params_nullable(self):
        """Test nullable parameter handling."""
        schema = {
            "name": {"type": str, "nullable": True},
            "age": {"type": int, "required": True, "nullable": False}
        }
        values = {"name": None, "age": 30}
        
        validated, errors = validate_params(schema, values)
        
        assert len(errors) == 0
        assert validated["name"] is None
        assert validated["age"] == 30

    def test_validate_params_string_to_int_coercion(self):
        """Test string to int coercion."""
        schema = {
            "count": {"type": int, "required": True}
        }
        values = {"count": "42"}
        
        validated, errors = validate_params(schema, values)
        
        assert len(errors) == 0
        assert validated["count"] == 42

    def test_validate_params_tuple_types(self):
        """Test tuple type checking."""
        schema = {
            "value": {"type": (str, int), "required": True}
        }
        
        # String is valid
        values = {"value": "test"}
        validated, errors = validate_params(schema, values)
        assert len(errors) == 0
        
        # Int is also valid
        values = {"value": 123}
        validated, errors = validate_params(schema, values)
        assert len(errors) == 0


class TestFormatErrors:
    """Test format_errors function."""

    def test_format_errors_single(self):
        """Test formatting single error."""
        errors = ["Error message"]
        result = format_errors(errors)
        assert result == "Error message"

    def test_format_errors_multiple(self):
        """Test formatting multiple errors."""
        errors = ["Error 1", "Error 2", "Error 3"]
        result = format_errors(errors)
        assert "Error 1" in result
        assert "Error 2" in result
        assert "Error 3" in result
        assert "; " in result  # Separator

    def test_format_errors_empty(self):
        """Test formatting empty error list."""
        result = format_errors([])
        assert result == ""
