import sys
import os
sys.path.insert(0, '/opt/codeagent')
import asyncio
from tools.image_gen import ImageGenTool

async def main():
    tool = ImageGenTool()
    prompt = "Glowing neon diamond floating above dark reflective metallic floor. Electric blue and purple neon light emitting from diamond, sharp geometric facets, metallic surface showing reflections, black background, 3D render, high detail."
    print("Initiating ImageGenTool execution...")
    result = await tool.execute(prompt=prompt)
    print("IMAGE GENERATOR EXECUTION COMPLETED!")
    print("RESULT:")
    print(result)

if __name__ == '__main__':
    asyncio.run(main())
