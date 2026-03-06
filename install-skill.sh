#!/bin/bash

# Simple script to install a skill from this repo to ~/.agents/skills/
# Usage: ./install-skill.sh <skill-name>
# Example: ./install-skill.sh handoff

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <skill-name>"
    echo "Available skills:"
    for dir in */; do
        if [ -f "${dir}SKILL.md" ]; then
            echo "  - ${dir%/}"
        fi
    done
    exit 1
fi

SKILL_NAME="$1"
SOURCE_DIR="$(pwd)/$SKILL_NAME"
TARGET_DIR="$HOME/.agents/skills/$SKILL_NAME"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "Error: Skill '$SKILL_NAME' not found in current directory"
    exit 1
fi

if [ ! -f "$SOURCE_DIR/SKILL.md" ]; then
    echo "Error: '$SKILL_NAME' is not a valid skill (missing SKILL.md)"
    exit 1
fi

echo "Installing $SKILL_NAME to $TARGET_DIR..."

# Create target directory if it doesn't exist
mkdir -p "$HOME/.agents/skills"

# Remove existing installation if present
if [ -d "$TARGET_DIR" ]; then
    echo "Removing existing installation..."
    rm -rf "$TARGET_DIR"
fi

# Copy skill directory
cp -r "$SOURCE_DIR" "$TARGET_DIR"

echo "✓ Successfully installed $SKILL_NAME"
echo "  Location: $TARGET_DIR"
