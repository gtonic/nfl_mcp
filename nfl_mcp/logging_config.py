"""
Structured logging configuration for the NFL MCP Server.

This module provides centralized logging configuration with structured logging
capabilities for better monitoring and debugging.
"""

import logging
import logging.config
import json
import sys
import time
from datetime import datetime, UTC
from typing import Dict, Any, Optional
from pathlib import Path


class StructuredFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs."""
    
    def __init__(self, service_name: str = "nfl-mcp-server", version: str = "0.1.0"):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as structured JSON."""
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "version": self.version,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "thread": record.thread,
            "thread_name": record.threadName,
        }
        
        # Add extra fields if present
        if hasattr(record, 'extra'):
            log_entry.update(record.extra)
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # Add stack info if present
        if record.stack_info:
            log_entry["stack_info"] = record.stack_info
        
        return json.dumps(log_entry, default=str)


class RequestFormatter(logging.Formatter):
    """Specialized formatter for HTTP request/response logging."""
    
    def __init__(self, service_name: str = "nfl-mcp-server"):
        super().__init__()
        self.service_name = service_name
    
    def format(self, record: logging.LogRecord) -> str:
        """Format HTTP request/response as structured JSON."""
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "type": "http_request",
            "message": record.getMessage()
        }
        
        # Add HTTP-specific fields if present
        for field in ['method', 'path', 'status_code', 'response_time_ms', 
                     'user_agent', 'client_ip', 'request_size', 'response_size']:
            if hasattr(record, field):
                log_entry[field] = getattr(record, field)
        
        return json.dumps(log_entry, default=str)


def setup_logging(
    log_level: str = "INFO",
    service_name: str = "nfl-mcp-server",
    version: str = "0.1.0",
    enable_file_logging: bool = True,
    log_file_path: Optional[str] = None
) -> None:
    """
    Setup structured logging configuration.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        service_name: Name of the service for log entries
        version: Version of the service
        enable_file_logging: Whether to enable file logging
        log_file_path: Path to log file (defaults to logs/nfl_mcp.log)
    """
    if log_file_path is None:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        log_file_path = str(log_dir / "nfl_mcp.log")
    
    # Ensure log directory exists
    Path(log_file_path).parent.mkdir(parents=True, exist_ok=True)
    
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structured": {
                "()": StructuredFormatter,
                "service_name": service_name,
                "version": version
            },
            "request": {
                "()": RequestFormatter,
                "service_name": service_name
            },
            "simple": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": log_level,
                "formatter": "structured",
                "stream": sys.stdout
            },
            "requests": {
                "class": "logging.StreamHandler", 
                "level": "INFO",
                "formatter": "request",
                "stream": sys.stdout
            }
        },
        "loggers": {
            "nfl_mcp": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False
            },
            "nfl_mcp.requests": {
                "level": "INFO",
                "handlers": ["requests"],
                "propagate": False
            },
            "httpx": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False
            },
            "uvicorn": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False
            }
        },
        "root": {
            "level": log_level,
            "handlers": ["console"]
        }
    }
    
    # Add file logging if enabled
    if enable_file_logging:
        config["handlers"]["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "level": log_level,
            "formatter": "structured",
            "filename": log_file_path,
            "maxBytes": 10 * 1024 * 1024,  # 10MB
            "backupCount": 5,
            "encoding": "utf8"
        }
        config["loggers"]["nfl_mcp"]["handlers"].append("file")
        config["root"]["handlers"].append("file")
    
    logging.config.dictConfig(config)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the given name.
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


def log_with_context(logger: logging.Logger, level: str, message: str, **context) -> None:
    """
    Log a message with additional context fields.
    
    Args:
        logger: Logger instance
        level: Log level (debug, info, warning, error, critical)
        message: Log message
        **context: Additional context fields to include in log
    """
    log_func = getattr(logger, level.lower())
    
    # Create a custom LogRecord with extra fields
    record = logger.makeRecord(
        logger.name,
        getattr(logging, level.upper()),
        "",
        0,
        message,
        (),
        None
    )
    
    # Add context as extra fields
    for key, value in context.items():
        setattr(record, key, value)
    
    logger.handle(record)


# Initialize logging on module import
setup_logging()