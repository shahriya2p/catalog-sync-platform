import subprocess
import os

def generate_explanation_context():
    """
    Gathers all git changes (tracked and untracked) into a single text file.
    This file can then be shared with a 3rd person or an LLM to explain the code.
    """
    output_file = 'code_changes_context.txt'
    
    # 1. Get the diff of tracked files against the last commit
    try:
        diff = subprocess.check_output(['git', 'diff', 'HEAD']).decode('utf-8')
    except subprocess.CalledProcessError:
        diff = "Could not retrieve git diff.\n"

    # 2. Get the list of untracked (new) files
    try:
        untracked = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard']).decode('utf-8').splitlines()
    except subprocess.CalledProcessError:
        untracked = []

    # 3. Write everything into a single context document
    with open(output_file, 'w') as f:
        f.write("CONTEXT FOR EXPLANATION:\n")
        f.write("========================\n")
        f.write("The following contains the git diff of existing files and the contents of newly added files.\n")
        f.write("Please review these changes and explain the overall architecture and modifications to a 3rd person.\n\n")
        
        f.write("MODIFIED FILES (DIFF):\n")
        f.write("======================\n")
        f.write(diff if diff else "No tracked changes.\n")
        f.write("\n")
        
        f.write("NEW UNTRACKED FILES:\n")
        f.write("====================\n")
        for uf in untracked:
            if os.path.isfile(uf) and not uf.endswith('.pyc'):
                f.write(f"\n--- {uf} ---\n")
                try:
                    with open(uf, 'r') as uf_f:
                        f.write(uf_f.read())
                except Exception as e:
                    f.write(f"<Could not read file: {e}>\n")
                f.write("\n")
                
    print(f"✅ Successfully generated '{output_file}'.")
    print("You can share this text file directly with a 3rd person or paste its contents into an AI (like ChatGPT or Gemini) to generate an executive summary or technical explanation.")

if __name__ == "__main__":
    generate_explanation_context()
