# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Audit a private Amlogic MBR image and explicitly described raw regions."""
import hashlib, struct, zlib
from pathlib import Path
from .config import require

def digest_region(stream, start, size):
    h=hashlib.sha256();stream.seek(start)
    while size:
        data=stream.read(min(size,1024**2));require(data,'Truncated image region')
        h.update(data);size-=len(data)
    return h.hexdigest()

def inspect(path, board, components):
    path=Path(path);size=path.stat().st_size
    require(size%512==0 and size>board['shared_start']*512,'Invalid MBR image capacity')
    expected=[(board['boot_start'],board['boot_sectors'],0x0c),
              (board['rescue_a_start'],board['rescue_a_sectors'],0x83),
              (board['rescue_b_start'],board['rescue_a_sectors'],0x83),
              (board['shared_start'],None,0x83)]
    with path.open('rb') as stream:
        mbr=stream.read(512)
        require(mbr[510:]==b'\x55\xaa' and mbr[:440]==bytes(440),'Unexpected MBR boot code')
        signature=struct.unpack_from('<I',mbr,440)[0];require(signature!=0,'Missing MBR signature')
        partitions=[]
        for i,(start,length,kind) in enumerate(expected):
            entry=mbr[446+i*16:462+i*16];actual_start,actual_size=struct.unpack_from('<II',entry,8)
            require(entry[4]==kind and actual_start==start and (length is None or actual_size==length),
                    'MBR partition differs from declared geometry')
            require(actual_size>0 and (start+actual_size)*512<=size,'MBR partition exceeds image')
            partitions.append({'number':i+1,'start':start,'sectors':actual_size,'type':kind,
                               'partuuid':f'{signature:08x}-{i+1:02x}'})
        regions=[(0,512)]
        for component in components:
            begin=component['offset_bytes'];end=begin+component['size_bytes']
            require(begin>=512 and begin%512==0 and end<=board['protected_prefix_bytes'],
                    'Boot component exceeds the protected prefix')
            require(not any(begin<right and end>left for left,right in regions),'Overlapping boot components')
            require(digest_region(stream,begin,component['size_bytes'])==component['sha256'],'Boot component hash differs')
            regions.append((begin,end))
        regions+= [(p['start']*512,(p['start']+p['sectors'])*512) for p in partitions]
        cursor=0
        for left,right in sorted(regions):
            require(left>=cursor,'Image regions overlap')
            if left>cursor:
                stream.seek(cursor);remaining=left-cursor
                while remaining:
                    block=stream.read(min(remaining,1024**2))
                    require(block and not any(block),'Undeclared payload in an image gap');remaining-=len(block)
            cursor=right
        require(cursor==size,'Unexpected unallocated image tail')
        stream.seek(partitions[0]['start']*512)
        fat=stream.read(512)
        require(fat[82:90]==b'FAT32   ' and fat[510:]==b'\x55\xaa','BOOT is not FAT32')
        for p in partitions[1:3]:
            stream.seek(p['start']*512);require(stream.read(4)==b'hsqs','Rescue slot is not SquashFS')
        stream.seek(partitions[3]['start']*512+1080);require(stream.read(2)==b'\x53\xef','Shared filesystem is not ext4')
    return {'partition_table':'dos','disk_signature':f'{signature:08x}','partitions':partitions,
            'protected_prefix_bytes':board['protected_prefix_bytes'],'components':components,'raw_gaps_zero':True,
            'public_redistribution_reviewed':False}
